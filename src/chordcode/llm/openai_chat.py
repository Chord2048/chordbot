from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from chordcode.log import log

try:
    from langfuse.openai import AsyncOpenAI
    LANGFUSE_AVAILABLE = True
except ImportError:
    from openai import AsyncOpenAI
    LANGFUSE_AVAILABLE = False


@dataclass(frozen=True)
class TextDelta:
    type: str
    text: str


@dataclass(frozen=True)
class ToolCall:
    type: str
    call_id: str
    name: str
    args_json: str


@dataclass(frozen=True)
class Finish:
    type: str
    reason: str


@dataclass(frozen=True)
class Error:
    type: str
    message: str


LLMEvent = TextDelta | ToolCall | Finish | Error


class OpenAIChatProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str, langfuse_enabled: bool = False) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._langfuse_enabled = langfuse_enabled and LANGFUSE_AVAILABLE
        
        if langfuse_enabled and not LANGFUSE_AVAILABLE:
            log.bind(event="llm.langfuse.unavailable").warning(
                "Langfuse requested but not available; using standard OpenAI client",
            )

    async def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        langfuse_trace_id: str | None = None,
        langfuse_parent_observation_id: str | None = None,
    ) -> AsyncIterator[LLMEvent]:
        opts = params or {}
        options = opts.get("options")
        extra = options if isinstance(options, dict) else {}
        temperature = opts.get("temperature")
        top_p = opts.get("top_p")

        kw: dict[str, Any] = {**extra}
        if isinstance(temperature, (int, float)):
            kw["temperature"] = float(temperature)
        if isinstance(top_p, (int, float)):
            kw["top_p"] = float(top_p)
        if headers:
            kw["extra_headers"] = headers

        # Add Langfuse context to properly nest the OpenAI generation under the session trace.
        # The Langfuse OpenAI integration expects the kwargs `trace_id` and `parent_observation_id`
        # (see `langfuse.openai.OpenAiArgsExtractor`), so we map our internal names to those.
        if self._langfuse_enabled:
            if langfuse_trace_id:
                kw["trace_id"] = langfuse_trace_id
            if langfuse_parent_observation_id:
                kw["parent_observation_id"] = langfuse_parent_observation_id

        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, *messages],
                tools=tools,
                tool_choice="auto",
                stream=True,
                **kw,
            )
        except Exception as e:
            yield Error(type="error", message=str(e))
            return

        calls: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None

        async for chunk in stream:
            choice = chunk.choices[0]
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta

            if delta.content:
                yield TextDelta(type="text_delta", text=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = int(tc.index)
                    c = calls.get(idx) or {"id": "", "name": "", "args": ""}
                    if tc.id:
                        c["id"] = tc.id
                    if tc.function and tc.function.name:
                        c["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        c["args"] += tc.function.arguments
                    calls[idx] = c

        if finish_reason:
            yield Finish(type="finish", reason=finish_reason)

        if finish_reason == "tool_calls":
            for c in calls.values():
                if c["id"] and c["name"]:
                    yield ToolCall(type="tool_call", call_id=c["id"], name=c["name"], args_json=c["args"])
