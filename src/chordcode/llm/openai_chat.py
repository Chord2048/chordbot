from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from chordcode.log import log_event

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
class ReasoningDelta:
    type: str
    text: str


@dataclass(frozen=True)
class Finish:
    type: str
    reason: str


@dataclass(frozen=True)
class Error:
    type: str
    message: str


LLMEvent = TextDelta | ReasoningDelta | ToolCall | Finish | Error


def _extract_delta_field(delta: Any, field: str) -> Any:
    direct = getattr(delta, field, None)
    if direct is not None:
        return direct
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get(field)
    return None


def _coerce_reasoning_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "reasoning_content", "content"):
            out = _coerce_reasoning_text(value.get(key))
            if out:
                return out
        return ""
    if isinstance(value, list):
        return "".join(_coerce_reasoning_text(item) for item in value)
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    content_attr = getattr(value, "content", None)
    if content_attr is not None:
        return _coerce_reasoning_text(content_attr)
    return ""


def _extract_reasoning_text(delta: Any) -> str:
    for field in ("reasoning_content", "reasoning"):
        text = _coerce_reasoning_text(_extract_delta_field(delta, field))
        if text:
            return text
    return ""


class OpenAIChatProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str, langfuse_enabled: bool = False) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._langfuse_enabled = langfuse_enabled and LANGFUSE_AVAILABLE
        
        if langfuse_enabled and not LANGFUSE_AVAILABLE:
            log_event(
                "Langfuse requested but not available; using standard OpenAI client",
                level="warning",
                event="llm.langfuse.unavailable",
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
        req_started = time.perf_counter()

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

        log_event(
            "Creating OpenAI-compatible streaming request",
            level="debug",
            event="llm.provider.request.start",
            model=self._model,
            system_chars=len(system),
            messages_count=len(messages),
            tools_count=len(tools),
            headers_count=len(headers or {}),
            temperature=kw.get("temperature"),
            top_p=kw.get("top_p"),
            langfuse_trace_attached=bool(langfuse_trace_id),
            langfuse_parent_attached=bool(langfuse_parent_observation_id),
        )

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
            log_event(
                "Failed to create OpenAI-compatible streaming request",
                level="error",
                event="llm.provider.request.error",
                exception=e,
                model=self._model,
                duration_ms=float((time.perf_counter() - req_started) * 1000),
            )
            yield Error(type="error", message=str(e))
            return
        log_event(
            "OpenAI-compatible streaming request created",
            level="debug",
            event="llm.provider.request.ready",
            model=self._model,
            duration_ms=float((time.perf_counter() - req_started) * 1000),
        )

        calls: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None
        chunk_count = 0
        text_delta_count = 0
        tool_delta_count = 0
        stream_started = time.perf_counter()

        try:
            async for chunk in stream:
                chunk_count += 1
                if not chunk.choices:
                    log_event(
                        "Received empty choices in streaming chunk",
                        level="warning",
                        event="llm.provider.chunk.empty",
                        model=self._model,
                        chunk_index=chunk_count,
                    )
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta

                if delta.content:
                    text_delta_count += 1
                    yield TextDelta(type="text_delta", text=delta.content)

                reasoning_text = _extract_reasoning_text(delta)
                if reasoning_text:
                    yield ReasoningDelta(type="reasoning_delta", text=reasoning_text)

                if delta.tool_calls:
                    tool_delta_count += len(delta.tool_calls)
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
        except Exception as e:
            log_event(
                "Failed while reading OpenAI-compatible stream",
                level="error",
                event="llm.provider.stream.error",
                exception=e,
                model=self._model,
                chunk_count=chunk_count,
                duration_ms=float((time.perf_counter() - stream_started) * 1000),
            )
            yield Error(type="error", message=str(e))
            return

        if finish_reason:
            yield Finish(type="finish", reason=finish_reason)

        emitted_tool_calls = 0
        if finish_reason == "tool_calls":
            for idx, c in calls.items():
                if c["id"] and c["name"]:
                    emitted_tool_calls += 1
                    yield ToolCall(type="tool_call", call_id=c["id"], name=c["name"], args_json=c["args"])
                    continue
                log_event(
                    "Dropped incomplete tool call from streaming response",
                    level="warning",
                    event="llm.provider.tool_call.incomplete",
                    model=self._model,
                    call_index=idx,
                    has_id=bool(c["id"]),
                    has_name=bool(c["name"]),
                    args_chars=len(c["args"]),
                )

        log_event(
            "OpenAI-compatible stream finished",
            level="debug",
            event="llm.provider.stream.finish",
            model=self._model,
            finish_reason=finish_reason,
            chunk_count=chunk_count,
            text_delta_count=text_delta_count,
            tool_delta_count=tool_delta_count,
            tool_calls_count=len(calls),
            emitted_tool_calls=emitted_tool_calls,
            duration_ms=float((time.perf_counter() - stream_started) * 1000),
        )
