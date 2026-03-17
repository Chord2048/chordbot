from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import uuid4

from chordcode.agents.types import AgentDefinition, AgentLimits, RunRequest
from chordcode.bus.bus import Bus, Event
from chordcode.config import Config
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker
from chordcode.llm.openai_chat import Error as LLMError
from chordcode.llm.openai_chat import Finish, OpenAIChatProvider, ReasoningDelta, TextDelta, ToolCall
from chordcode.log import logger
from chordcode.loop.interrupt import InterruptManager
from chordcode.model import (
    Message,
    MessageWithParts,
    ModelRef,
    PermissionRule,
    ReasoningPart,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
    ToolStatePending,
    ToolStateRunning,
)
from chordcode.observability.langfuse_client import get_langfuse
from chordcode.permission.service import PermissionRejected, PermissionService
from chordcode.prompts.template import render_prompt
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.base import ToolResult
from chordcode.tools.registry import ToolInfo, ToolRegistry


@dataclass(frozen=True)
class ToolCtx:
    session_id: str
    message_id: str
    agent: str
    source: str
    bus: Bus
    store: SQLiteStore
    perm: PermissionService
    ruleset: list[PermissionRule]
    tool_part_id: str
    trace_id: str | None = None
    parent_observation_id: str | None = None
    root_session_id: str | None = None
    parent_session_id: str | None = None
    parallel_group_id: str | None = None
    parallel_index: int | None = None
    parallel_size: int | None = None

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict[str, Any]) -> None:
        ask_metadata = dict(metadata)
        ask_metadata.setdefault("source", self.source)
        await self.perm.ask(
            session_id=self.session_id,
            ruleset=self.ruleset,
            permission=permission,
            patterns=patterns,
            always=always,
            metadata=ask_metadata,
            tool={"message_id": self.message_id, "call_id": self.tool_part_id},
        )

    async def tool_stream_update(self, output: str) -> None:
        await self.bus.publish(
            Event(
                type="message.part.updated",
                properties={
                    "session_id": self.session_id,
                    "message_id": self.message_id,
                    "part": {
                        "type": "tool_stream",
                        "call_id": self.tool_part_id,
                        "output": output,
                    },
                },
            ),
        )


@dataclass(frozen=True)
class _PreparedToolCall:
    index: int
    call_id: str
    tool_name: str
    part_id: str
    args: dict[str, Any]
    start_ms: int
    tool: Any
    ctx: ToolCtx


@dataclass(frozen=True)
class _ToolExecutionOutcome:
    call_id: str
    tool_name: str
    part_id: str
    input: dict[str, Any]
    title: str
    output: str
    metadata: dict[str, Any]
    start_ms: int
    end_ms: int


class SessionLoop:
    def __init__(
        self,
        *,
        cfg: Config,
        bus: Bus,
        store: SQLiteStore,
        perm: PermissionService,
        tools: ToolRegistry,
        llm: OpenAIChatProvider,
        interrupt: InterruptManager,
        hooks: Hooker | None = None,
        agent_definition: AgentDefinition | None = None,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._perm = perm
        self._tools = tools
        self._llm = llm
        self._interrupt = interrupt
        self._hooks = hooks
        self._agent_definition = agent_definition
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def run(
        self,
        *,
        session_id: str | None = None,
        source: str = "api",
        request: RunRequest | None = None,
    ) -> tuple[str, str | None] | tuple[str, str | None, str | None]:
        if request is None:
            if not session_id:
                raise ValueError("session_id is required when request is not provided")
            agent_name = self._agent_definition.name if self._agent_definition else "primary"
            request = RunRequest(
                session_id=session_id,
                agent_name=agent_name,
                source=source,
                root_session_id=session_id,
                limits=self._agent_definition.limits if self._agent_definition else AgentLimits(),
            )
            assistant_id, trace_id, _finish = await self._run_request(request)
            return assistant_id, trace_id
        return await self._run_request(request)

    async def _run_request(self, request: RunRequest) -> tuple[str, str | None, str | None]:
        run_log = logger.child(
            session_id=request.session_id,
            root_session_id=request.root_session_id or request.session_id,
            parent_session_id=request.parent_session_id,
            agent=request.agent_name,
            trace_id=request.trace_id,
        )
        run_log.debug("Session run requested", event="session.run")
        async with self._lock(request.session_id):
            await self._interrupt.clear(request.session_id)

            langfuse = get_langfuse()
            if not langfuse:
                return await self._run_session_with_trace(request, None, request.trace_id)

            trace_kwargs: dict[str, Any] = {
                "as_type": "span",
                "name": "agent-session",
                "metadata": {
                    "agent": request.agent_name,
                    "session_id": request.session_id,
                    "source": request.source,
                    "root_session_id": request.root_session_id or request.session_id,
                    "parent_session_id": request.parent_session_id,
                    "parent_tool_call_id": request.parent_tool_call_id,
                },
            }
            if request.trace_id:
                trace_kwargs["trace_id"] = request.trace_id
            if request.parent_observation_id:
                trace_kwargs["parent_observation_id"] = request.parent_observation_id

            try:
                with langfuse.start_as_current_observation(**trace_kwargs) as trace_span:
                    trace_id = request.trace_id or getattr(trace_span, "trace_id", None)
                    run_log.child(trace_id=trace_id).debug("Langfuse trace started", event="langfuse.trace.start")
                    return await self._run_session_with_trace(request, trace_span, trace_id)
            except TypeError as exc:
                run_log.warning(
                    "Langfuse observation did not accept nested trace kwargs; falling back to root span",
                    event="langfuse.trace.start.fallback",
                    error=str(exc),
                )
                try:
                    fallback_kwargs = {
                        "as_type": "span",
                        "name": "agent-session",
                        "metadata": trace_kwargs["metadata"],
                    }
                    with langfuse.start_as_current_observation(**fallback_kwargs) as trace_span:
                        trace_id = request.trace_id or getattr(trace_span, "trace_id", None)
                        return await self._run_session_with_trace(request, trace_span, trace_id)
                except Exception as inner_exc:
                    run_log.error("Error creating Langfuse trace span", event="langfuse.trace.start.error", exc_info=inner_exc)
                    return await self._run_session_with_trace(request, None, request.trace_id)
            except Exception as exc:
                run_log.error("Error creating Langfuse trace span", event="langfuse.trace.start.error", exc_info=exc)
                return await self._run_session_with_trace(request, None, request.trace_id)

    async def _run_session_with_trace(
        self,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
    ) -> tuple[str, str | None, str | None]:
        session_id = request.session_id
        agent = request.agent_name
        session_log = logger.child(
            session_id=session_id,
            root_session_id=request.root_session_id or session_id,
            parent_session_id=request.parent_session_id,
            trace_id=trace_id,
            agent=agent,
        )
        busy_published = False
        assistant: Message | None = None
        buffers: dict[str, TextPart | ReasoningPart | None] = {"text": None, "reasoning": None}
        try:
            session = await self._store.get_session(session_id)
            model = self._agent_definition.model_override if self._agent_definition and self._agent_definition.model_override else ModelRef(provider="openai-compatible", id=self._cfg.openai.model)
            session_log.info("Session started", event="session.start", worktree=session.worktree, cwd=session.cwd, model=model.id)

            if trace_span:
                try:
                    trace_span.update(
                        metadata={
                            "agent": agent,
                            "worktree": session.worktree,
                            "cwd": session.cwd,
                            "model": model.id,
                            "root_session_id": request.root_session_id or session_id,
                            "parent_session_id": request.parent_session_id,
                        }
                    )
                except Exception as exc:
                    session_log.error("Error updating Langfuse trace metadata", event="langfuse.trace.update.error", exc_info=exc)

            await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "busy"}))
            busy_published = True

            history = await self._store.list_messages(session_id)
            user = next((m for m in reversed(history) if m.info.role == "user"), None)
            if not user:
                raise RuntimeError("no user message")

            assistant = Message(
                id=str(uuid4()),
                session_id=session_id,
                role="assistant",
                parent_id=user.info.id,
                agent=agent,
                model=model,
                created_at=int(time.time() * 1000),
            )
            await self._store.add_message(assistant)
            await self._bus.publish(
                Event(type="message.updated", properties={"session_id": session_id, "info": assistant.model_dump()})
            )

            tool_infos = self._tools.list()
            tool_specs = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.schema,
                    },
                }
                for tool in tool_infos
            ]

            messages = self._to_openai_messages(history)
            turn_index = 0
            executed_tool_calls = 0

            while True:
                max_turns = request.limits.max_turns or 0
                if max_turns > 0 and turn_index >= max_turns:
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish="max_turns_exceeded",
                    )

                turn_index += 1
                turn_log = session_log.child(message_id=assistant.id)
                turn_log.debug(
                    "Session turn started",
                    event="session.turn.start",
                    turn=turn_index,
                    history_messages=len(messages),
                )

                if await self._interrupt.is_interrupted(session_id):
                    signal = await self._interrupt.check(session_id)
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=signal.reason if signal else "interrupted",
                    )

                system = await self._build_system_prompt(session, agent, model)
                msgs = await self._transform_messages(session_id, agent, model, messages)
                params, hdrs = await self._build_params_and_headers(session_id, agent, model, user)

                messages_count = len(msgs)
                messages_chars = 0
                for msg in msgs:
                    content = msg.get("content")
                    if isinstance(content, str):
                        messages_chars += len(content)
                turn_log.debug(
                    "LLM request",
                    event="llm.request",
                    system_chars=len(system),
                    messages_count=messages_count,
                    messages_chars=messages_chars,
                    tools_count=len(tool_specs),
                    temperature=params.get("temperature"),
                    top_p=params.get("top_p"),
                )

                calls: list[ToolCall] = []
                reason: str | None = None
                interrupted_during_stream = False
                try:
                    async for evt in self._llm.stream(
                        system=system,
                        messages=msgs,
                        tools=tool_specs,
                        params=params,
                        headers=hdrs,
                        langfuse_trace_id=trace_id,
                        langfuse_parent_observation_id=getattr(trace_span, "id", None) or request.parent_observation_id,
                    ):
                        if await self._interrupt.is_interrupted(session_id):
                            interrupted_during_stream = True
                            break
                        if isinstance(evt, TextDelta):
                            await self._append_text_delta(assistant.id, session_id, buffers, evt.text)
                            continue
                        if isinstance(evt, ReasoningDelta):
                            await self._append_reasoning_delta(assistant.id, session_id, buffers, evt.text)
                            continue
                        if isinstance(evt, ToolCall):
                            turn_log.child(tool_name=evt.name, tool_call_id=evt.call_id).debug(
                                "LLM emitted tool call",
                                event="llm.tool_call.received",
                                args_json_chars=len(evt.args_json or ""),
                            )
                            calls.append(evt)
                            continue
                        if isinstance(evt, LLMError):
                            turn_log.error("LLM returned error event", event="llm.response.error", error_message=evt.message)
                            raise RuntimeError(f"LLM provider error: {evt.message}")
                        if isinstance(evt, Finish):
                            reason = evt.reason
                            await self._flush_buffered_parts(session_id, assistant.id, buffers)
                            turn_log.debug(
                                "LLM finished",
                                event="llm.finish",
                                finish_reason=reason,
                            )
                except asyncio.CancelledError:
                    await self._flush_buffered_parts(session_id, assistant.id, buffers)
                    signal = await self._interrupt.check(session_id)
                    finish = signal.reason if signal else "interrupted"
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=finish,
                    )
                except Exception as exc:
                    await self._flush_buffered_parts(session_id, assistant.id, buffers)
                    turn_log.error("LLM streaming error", event="llm.stream.error", exc_info=exc)
                    await self._bus.publish(
                        Event(
                            type="session.error",
                            properties={
                                "session_id": session_id,
                                "error": {
                                    "message": str(exc),
                                    "type": type(exc).__name__,
                                    "context": "llm_stream",
                                },
                            },
                        )
                    )
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish="error",
                        error={"message": str(exc), "type": type(exc).__name__},
                    )

                if interrupted_during_stream:
                    turn_log.info("LLM stream interrupted by user signal", event="llm.stream.interrupted", turn=turn_index)
                    signal = await self._interrupt.check(session_id)
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=signal.reason if signal else "interrupted",
                    )

                if calls:
                    max_tool_calls = request.limits.max_tool_calls or 0
                    if max_tool_calls > 0 and executed_tool_calls + len(calls) > max_tool_calls:
                        return await self._finish_assistant(
                            session_id=session_id,
                            assistant=assistant,
                            trace_id=trace_id,
                            finish="max_tool_calls_exceeded",
                        )

                    turn_log.debug(
                        "Processing tool calls",
                        event="llm.tool_calls.batch",
                        tool_calls_count=len(calls),
                        finish_reason=reason,
                    )
                    blocked = await self._execute_tool_batch(
                        session=session,
                        assistant=assistant,
                        model=model,
                        request=request,
                        trace_span=trace_span,
                        trace_id=trace_id,
                        turn_log=turn_log,
                        calls=calls,
                    )
                    executed_tool_calls += len(calls)
                    if blocked:
                        return await self._finish_assistant(
                            session_id=session_id,
                            assistant=assistant,
                            trace_id=trace_id,
                            finish="blocked",
                        )
                    history = await self._store.list_messages(session_id)
                    messages = self._to_openai_messages(history)
                    turn_log.debug(
                        "Session turn continuing after tool execution",
                        event="session.turn.next",
                        turn=turn_index,
                        history_messages=len(messages),
                    )
                    continue

                if reason and reason != "tool_calls":
                    turn_log.info("Session finished", event="session.finish", finish_reason=reason, turn=turn_index)
                    return await self._finish_assistant(
                        session_id=session_id,
                        assistant=assistant,
                        trace_id=trace_id,
                        finish=reason,
                    )
        except Exception as exc:
            if trace_span:
                try:
                    trace_span.update(level="ERROR", metadata={"error_type": type(exc).__name__, "error_message": str(exc)})
                except Exception as inner_exc:
                    session_log.error(
                        "Error updating Langfuse trace with error metadata",
                        event="langfuse.trace.error_update.error",
                        exc_info=inner_exc,
                    )
            session_log.error("Session failed early", event="session.error", exc_info=exc)
            await self._bus.publish(
                Event(
                    type="session.error",
                    properties={"session_id": session_id, "error": {"message": str(exc), "type": type(exc).__name__}},
                )
            )
            raise
        finally:
            if busy_published:
                await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))

    async def _build_system_prompt(self, session: Session, agent: str, model: ModelRef) -> str:
        prompt_worktree = session.worktree
        if session.runtime.backend == "daytona" and (not prompt_worktree or prompt_worktree == self._cfg.default_worktree):
            prompt_worktree = self._cfg.daytona.default_workspace or "/workspace"

        system = render_prompt(
            self._cfg.system_prompt,
            session_context={
                "session_id": session.id,
                "cwd": session.cwd,
                "worktree": prompt_worktree,
                "model": model.id,
                "agent": agent,
            },
            template_variables=self._cfg.prompt_templates,
        )
        if self._agent_definition:
            extra = self._agent_definition.load_prompt().strip()
            if extra:
                system = f"{system}\n\n{extra}"
        if self._hooks:
            sysout: dict[str, object] = {"system": [system]}
            await self._hooks.trigger(
                Hook.ExperimentalChatSystemTransform,
                {"session_id": session.id, "agent": agent, "model": model.model_dump()},
                sysout,
            )
            raw = sysout.get("system")
            if isinstance(raw, list):
                parts = [item for item in raw if isinstance(item, str)]
                if parts:
                    system = "\n\n".join(parts)
        return system

    async def _transform_messages(
        self,
        session_id: str,
        agent: str,
        model: ModelRef,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = messages
        if self._hooks:
            mout: dict[str, object] = {"messages": msgs}
            await self._hooks.trigger(
                Hook.ExperimentalChatMessagesTransform,
                {"session_id": session_id, "agent": agent, "model": model.model_dump()},
                mout,
            )
            raw = mout.get("messages")
            if isinstance(raw, list):
                msgs = cast(list[dict[str, Any]], raw)
        return msgs

    async def _build_params_and_headers(
        self,
        session_id: str,
        agent: str,
        model: ModelRef,
        user: MessageWithParts,
    ) -> tuple[dict[str, object], dict[str, str]]:
        params: dict[str, object] = {"temperature": 1.0, "top_p": 0.95, "top_k": 0, "options": {}}
        headers: dict[str, object] = {"headers": {}}
        if self._hooks:
            user_text = "".join([part.text for part in user.parts if getattr(part, "type", "") == "text"])
            await self._hooks.trigger(
                Hook.ChatParams,
                {
                    "session_id": session_id,
                    "agent": agent,
                    "model": model.model_dump(),
                    "message_id": user.info.id,
                    "message": user_text,
                },
                params,
            )
            await self._hooks.trigger(
                Hook.ChatHeaders,
                {
                    "session_id": session_id,
                    "agent": agent,
                    "model": model.model_dump(),
                    "message_id": user.info.id,
                    "message": user_text,
                },
                headers,
            )
        raw_headers = headers.get("headers")
        return params, cast(dict[str, str], raw_headers if isinstance(raw_headers, dict) else {})

    async def _append_text_delta(
        self,
        message_id: str,
        session_id: str,
        buffers: dict[str, TextPart | ReasoningPart | None],
        delta: str,
    ) -> None:
        current = cast(TextPart | None, buffers["text"])
        if current is None:
            current = TextPart(
                id=str(uuid4()),
                message_id=message_id,
                session_id=session_id,
                text=delta,
                time={"start": int(time.time() * 1000)},
            )
        else:
            current.text += delta
        buffers["text"] = current
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={
                    "session_id": session_id,
                    "message_id": message_id,
                    "part": current.model_dump(),
                    "delta": delta,
                },
            )
        )

    async def _append_reasoning_delta(
        self,
        message_id: str,
        session_id: str,
        buffers: dict[str, TextPart | ReasoningPart | None],
        delta: str,
    ) -> None:
        current = cast(ReasoningPart | None, buffers["reasoning"])
        if current is None:
            current = ReasoningPart(
                id=str(uuid4()),
                message_id=message_id,
                session_id=session_id,
                text=delta,
                time={"start": int(time.time() * 1000), "end": int(time.time() * 1000)},
            )
        else:
            current.text += delta
            current.time["end"] = int(time.time() * 1000)
        buffers["reasoning"] = current
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={
                    "session_id": session_id,
                    "message_id": message_id,
                    "part": current.model_dump(),
                    "delta": delta,
                },
            )
        )

    async def _flush_buffered_parts(
        self,
        session_id: str,
        message_id: str,
        buffers: dict[str, TextPart | ReasoningPart | None],
    ) -> None:
        reasoning = cast(ReasoningPart | None, buffers["reasoning"])
        if reasoning is not None:
            end_ms = int(time.time() * 1000)
            reasoning.time = {
                "start": reasoning.time.get("start", end_ms),
                "end": end_ms,
            }
            await self._store.add_part(session_id, message_id, reasoning)
            buffers["reasoning"] = None

        text = cast(TextPart | None, buffers["text"])
        if text is not None:
            end_ms = int(time.time() * 1000)
            text.time = {
                "start": (text.time or {}).get("start", end_ms),
                "end": end_ms,
            }
            await self._store.add_part(session_id, message_id, text)
            buffers["text"] = None

    async def _finish_assistant(
        self,
        *,
        session_id: str,
        assistant: Message,
        trace_id: str | None,
        finish: str,
        error: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, str]:
        completed_at = int(time.time() * 1000)
        await self._store.update_message(assistant.id, completed_at=completed_at, finish=finish, error=error)
        info = assistant.model_dump()
        info["completed_at"] = completed_at
        info["finish"] = finish
        if error is not None:
            info["error"] = error
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": info}))
        return assistant.id, trace_id, finish

    async def _execute_tool_batch(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
        turn_log: Any,
        calls: list[ToolCall],
    ) -> bool:
        if self._is_parallel_task_batch(calls):
            return await self._execute_parallel_tool_batch(
                session=session,
                assistant=assistant,
                model=model,
                request=request,
                trace_span=trace_span,
                trace_id=trace_id,
                turn_log=turn_log,
                calls=calls,
            )
        return await self._execute_sequential_tool_batch(
            session=session,
            assistant=assistant,
            model=model,
            request=request,
            trace_span=trace_span,
            trace_id=trace_id,
            turn_log=turn_log,
            calls=calls,
        )

    async def _execute_sequential_tool_batch(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
        turn_log: Any,
        calls: list[ToolCall],
    ) -> bool:
        for index, call in enumerate(calls):
            prepared, immediate = await self._prepare_tool_call(
                session=session,
                assistant=assistant,
                request=request,
                trace_id=trace_id,
                turn_log=turn_log,
                call=call,
                index=index,
                parallel_group_id=None,
                parallel_index=None,
                parallel_size=None,
            )
            if immediate is not None:
                await self._persist_tool_outcome(session.id, assistant, model, immediate)
                continue
            if prepared is None:
                continue
            try:
                outcome = await self._execute_prepared_tool_call(
                    prepared=prepared,
                    request=request,
                    trace_span=trace_span,
                    turn_log=turn_log,
                )
            except PermissionRejected:
                return True
            await self._persist_tool_outcome(session.id, assistant, model, outcome)
        return False

    async def _execute_parallel_tool_batch(
        self,
        *,
        session: Session,
        assistant: Message,
        model: ModelRef,
        request: RunRequest,
        trace_span: Any | None,
        trace_id: str | None,
        turn_log: Any,
        calls: list[ToolCall],
    ) -> bool:
        parallel_group_id = str(uuid4())
        outcomes: list[_ToolExecutionOutcome | BaseException | None] = [None] * len(calls)
        prepared_calls: list[_PreparedToolCall] = []
        parallel_size = len(calls)

        for index, call in enumerate(calls):
            prepared, immediate = await self._prepare_tool_call(
                session=session,
                assistant=assistant,
                request=request,
                trace_id=trace_id,
                turn_log=turn_log,
                call=call,
                index=index,
                parallel_group_id=parallel_group_id,
                parallel_index=index + 1,
                parallel_size=parallel_size,
            )
            if immediate is not None:
                outcomes[index] = immediate
                continue
            if prepared is not None:
                prepared_calls.append(prepared)

        results = await asyncio.gather(
            *[
                self._execute_prepared_tool_call(
                    prepared=prepared,
                    request=request,
                    trace_span=trace_span,
                    turn_log=turn_log,
                )
                for prepared in prepared_calls
            ],
            return_exceptions=True,
        )
        for prepared, result in zip(prepared_calls, results):
            outcomes[prepared.index] = cast(_ToolExecutionOutcome | BaseException, result)

        blocked = False
        for outcome in outcomes:
            if outcome is None:
                continue
            if isinstance(outcome, PermissionRejected):
                blocked = True
                continue
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                raise outcome
            await self._persist_tool_outcome(session.id, assistant, model, outcome)
        return blocked

    async def _prepare_tool_call(
        self,
        *,
        session: Session,
        assistant: Message,
        request: RunRequest,
        trace_id: str | None,
        turn_log: Any,
        call: ToolCall,
        index: int,
        parallel_group_id: str | None,
        parallel_index: int | None,
        parallel_size: int | None,
    ) -> tuple[_PreparedToolCall | None, _ToolExecutionOutcome | None]:
        call_id = call.call_id
        tool_name = call.name
        raw = call.args_json
        tool_log = turn_log.child(tool_name=tool_name, tool_call_id=call_id)

        part_id = str(uuid4())
        try:
            args = json.loads(raw or "{}")
        except Exception as exc:
            pending = ToolPart(
                id=part_id,
                message_id=assistant.id,
                session_id=session.id,
                call_id=call_id,
                tool=tool_name,
                state=ToolStatePending(input={}, raw=raw),
            )
            await self._store.add_part(session.id, assistant.id, pending)
            await self._bus.publish(
                Event(
                    type="message.part.updated",
                    properties={"session_id": session.id, "message_id": assistant.id, "part": pending.model_dump()},
                )
            )
            raw_preview = (raw or "")[:200]
            tool_log.error(
                "Failed to parse tool arguments",
                event="tool.args.parse.error",
                exc_info=exc,
                args_json_chars=len(raw or ""),
                args_json_preview=raw_preview,
            )
            return None, self._immediate_tool_outcome(
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                input={},
                output=f"Invalid tool arguments: {exc}",
            )

        pending = ToolPart(
            id=part_id,
            message_id=assistant.id,
            session_id=session.id,
            call_id=call_id,
            tool=tool_name,
            state=ToolStatePending(input=args, raw=raw),
        )
        await self._store.add_part(session.id, assistant.id, pending)
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session.id, "message_id": assistant.id, "part": pending.model_dump()},
            )
        )

        if self._hooks:
            out: dict[str, object] = {"args": args}
            await self._hooks.trigger(
                Hook.ToolExecuteBefore,
                {"tool": tool_name, "session_id": session.id, "call_id": call_id},
                out,
            )
            raw_args = out.get("args")
            if isinstance(raw_args, dict):
                args = cast(dict[str, Any], raw_args)

        start_ms = int(time.time() * 1000)
        safe_args: dict[str, Any] = {}
        if isinstance(args, dict):
            safe_args["args_keys"] = list(args.keys())
            safe_args["string_value_chars"] = {
                key: len(value) for key, value in args.items() if isinstance(key, str) and isinstance(value, str)
            }
        tool_log.debug("Tool execution started", event="tool.start", **safe_args)

        running = ToolPart(
            id=part_id,
            message_id=assistant.id,
            session_id=session.id,
            call_id=call_id,
            tool=tool_name,
            state=ToolStateRunning(input=args, time={"start": start_ms}),
        )
        await self._store.add_part(session.id, assistant.id, running)
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session.id, "message_id": assistant.id, "part": running.model_dump()},
            )
        )

        try:
            tool = self._tools.get(tool_name)
        except Exception as exc:
            tool_log.error("Tool lookup failed", event="tool.lookup.error", exc_info=exc)
            return None, self._immediate_tool_outcome(
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                input=args,
                output=f"Tool not found: {tool_name}",
                title=tool_name,
                start_ms=start_ms,
            )

        ctx = ToolCtx(
            session_id=session.id,
            message_id=assistant.id,
            agent=request.agent_name,
            source=request.source,
            bus=self._bus,
            store=self._store,
            perm=self._perm,
            ruleset=session.permission_rules,
            tool_part_id=call_id,
            trace_id=trace_id,
            root_session_id=request.root_session_id or session.id,
            parent_session_id=request.parent_session_id,
            parallel_group_id=parallel_group_id,
            parallel_index=parallel_index,
            parallel_size=parallel_size,
        )
        return (
            _PreparedToolCall(
                index=index,
                call_id=call_id,
                tool_name=tool_name,
                part_id=part_id,
                args=args,
                start_ms=start_ms,
                tool=tool,
                ctx=ctx,
            ),
            None,
        )

    async def _execute_prepared_tool_call(
        self,
        *,
        prepared: _PreparedToolCall,
        request: RunRequest,
        trace_span: Any | None,
        turn_log: Any,
    ) -> _ToolExecutionOutcome:
        tool_log = turn_log.child(tool_name=prepared.tool_name, tool_call_id=prepared.call_id)
        tool_span_cm = nullcontext(None)
        if trace_span:
            try:
                tool_span_cm = trace_span.start_as_current_observation(
                    as_type="tool",
                    name=f"tool-{prepared.tool_name}",
                    input=prepared.args,
                    metadata={
                        "tool": prepared.tool_name,
                        "call_id": prepared.call_id,
                        "parallel_group_id": prepared.ctx.parallel_group_id,
                        "parallel_index": prepared.ctx.parallel_index,
                        "parallel_size": prepared.ctx.parallel_size,
                    },
                )
            except Exception as exc:
                tool_log.error("Error creating Langfuse tool span", event="langfuse.tool_span.start.error", exc_info=exc)

        with tool_span_cm as tool_span:
            exec_ctx = replace(
                prepared.ctx,
                parent_observation_id=getattr(tool_span, "id", None) or prepared.ctx.parent_observation_id,
            )
            try:
                result = await prepared.tool.execute(prepared.args, exec_ctx)
                if tool_span:
                    try:
                        tool_span.update(output=result.output, metadata={**result.metadata, "title": result.title})
                    except Exception as exc:
                        tool_log.error("Error updating Langfuse tool span", event="langfuse.tool_span.update.error", exc_info=exc)
                out: dict[str, object] = {"title": result.title, "output": result.output, "metadata": result.metadata}
                if self._hooks:
                    await self._hooks.trigger(
                        Hook.ToolExecuteAfter,
                        {"tool": prepared.tool_name, "session_id": prepared.ctx.session_id, "call_id": prepared.call_id},
                        out,
                    )
                title = str(out.get("title") or result.title)
                output = str(out.get("output") or result.output)
                raw_meta = out.get("metadata")
                metadata = cast(dict[str, Any], raw_meta if isinstance(raw_meta, dict) else result.metadata)
                end_ms = int(time.time() * 1000)
                tool_log.debug(
                    "Tool execution finished",
                    event="tool.finish",
                    duration_ms=float(end_ms - prepared.start_ms),
                    output_chars=len(output),
                )
                return _ToolExecutionOutcome(
                    call_id=prepared.call_id,
                    tool_name=prepared.tool_name,
                    part_id=prepared.part_id,
                    input=prepared.args,
                    title=title,
                    output=output,
                    metadata=metadata,
                    start_ms=prepared.start_ms,
                    end_ms=end_ms,
                )
            except PermissionRejected as exc:
                if tool_span:
                    try:
                        tool_span.update(level="ERROR", metadata={"error": "PermissionRejected", "message": str(exc)})
                    except Exception as inner_exc:
                        tool_log.error(
                            "Error updating Langfuse tool span with PermissionRejected",
                            event="langfuse.tool_span.error_update.error",
                            exc_info=inner_exc,
                        )
                tool_log.warning("Tool blocked by permission", event="tool.blocked")
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if tool_span:
                    try:
                        tool_span.update(level="ERROR", metadata={"error": type(exc).__name__, "message": str(exc)})
                    except Exception as inner_exc:
                        tool_log.error(
                            "Error updating Langfuse tool span with error",
                            event="langfuse.tool_span.error_update.error",
                            exc_info=inner_exc,
                        )
                output = f"Tool execution failed: {exc}"
                out = {"title": prepared.tool_name, "output": output, "metadata": {"error": True}}
                if self._hooks:
                    await self._hooks.trigger(
                        Hook.ToolExecuteAfter,
                        {"tool": prepared.tool_name, "session_id": prepared.ctx.session_id, "call_id": prepared.call_id},
                        out,
                    )
                title = str(out.get("title") or prepared.tool_name)
                final_output = str(out.get("output") or output)
                raw_meta = out.get("metadata")
                metadata = cast(dict[str, Any], raw_meta if isinstance(raw_meta, dict) else {"error": True})
                end_ms = int(time.time() * 1000)
                tool_log.error(
                    "Tool execution failed",
                    event="tool.error",
                    duration_ms=float(end_ms - prepared.start_ms),
                    exc_info=exc,
                    output_chars=len(final_output),
                )
                return _ToolExecutionOutcome(
                    call_id=prepared.call_id,
                    tool_name=prepared.tool_name,
                    part_id=prepared.part_id,
                    input=prepared.args,
                    title=title,
                    output=final_output,
                    metadata=metadata,
                    start_ms=prepared.start_ms,
                    end_ms=end_ms,
                )

    async def _persist_tool_outcome(
        self,
        session_id: str,
        assistant: Message,
        model: ModelRef,
        outcome: _ToolExecutionOutcome,
    ) -> None:
        done = ToolPart(
            id=outcome.part_id,
            message_id=assistant.id,
            session_id=session_id,
            call_id=outcome.call_id,
            tool=outcome.tool_name,
            state=ToolStateCompleted(
                input=outcome.input,
                title=outcome.title,
                output=outcome.output,
                metadata=outcome.metadata,
                time={"start": outcome.start_ms, "end": outcome.end_ms},
            ),
        )
        await self._store.add_part(session_id, assistant.id, done)
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session_id, "message_id": assistant.id, "part": done.model_dump()},
            )
        )

        tool_msg_id = str(uuid4())
        tool_msg = Message(
            id=tool_msg_id,
            session_id=session_id,
            role="tool",
            parent_id=assistant.id,
            agent=assistant.agent,
            model=model,
            created_at=int(time.time() * 1000),
            tool_call_id=outcome.call_id,
            tool_name=outcome.tool_name,
        )
        await self._store.add_message(tool_msg)
        tool_text_part = TextPart(
            id=str(uuid4()),
            message_id=tool_msg_id,
            session_id=session_id,
            text=outcome.output,
            synthetic=True,
        )
        await self._store.add_part(session_id, tool_msg_id, tool_text_part)
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": tool_msg.model_dump()}))

    def _immediate_tool_outcome(
        self,
        *,
        call_id: str,
        tool_name: str,
        part_id: str,
        input: dict[str, Any],
        output: str,
        title: str | None = None,
        start_ms: int | None = None,
    ) -> _ToolExecutionOutcome:
        now = int(time.time() * 1000)
        return _ToolExecutionOutcome(
            call_id=call_id,
            tool_name=tool_name,
            part_id=part_id,
            input=input,
            title=title or tool_name,
            output=output,
            metadata={"error": True},
            start_ms=start_ms or now,
            end_ms=now,
        )

    def _is_parallel_task_batch(self, calls: list[ToolCall]) -> bool:
        return 2 <= len(calls) <= 3 and all(call.name == "task" for call in calls)

    def _to_openai_messages(self, history: list[MessageWithParts]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in history:
            if message.info.role == "user":
                txt = "".join([part.text for part in message.parts if getattr(part, "type", "") == "text"])
                out.append({"role": "user", "content": txt})
                continue
            if message.info.role == "assistant":
                txt = "".join([part.text for part in message.parts if getattr(part, "type", "") == "text"])
                reasoning = "".join([part.text for part in message.parts if getattr(part, "type", "") == "reasoning"])
                calls: dict[str, dict[str, Any]] = {}
                for part in message.parts:
                    if getattr(part, "type", "") != "tool":
                        continue
                    state = getattr(part, "state", None)
                    args_json = ""
                    if state and getattr(state, "status", "") == "pending":
                        args_json = str(getattr(state, "raw", "") or "")
                    if not args_json and state and getattr(state, "input", None) is not None:
                        args_json = json.dumps(getattr(state, "input"))
                    calls[part.call_id] = {
                        "id": part.call_id,
                        "type": "function",
                        "function": {"name": part.tool, "arguments": args_json},
                    }
                msg: dict[str, Any] = {"role": "assistant", "content": txt or ""}
                if reasoning:
                    msg["reasoning_content"] = reasoning
                if calls:
                    msg["tool_calls"] = list(calls.values())
                out.append(msg)
                continue
            if message.info.role == "tool":
                txt = "".join([part.text for part in message.parts if getattr(part, "type", "") == "text"])
                out.append({"role": "tool", "tool_call_id": message.info.tool_call_id, "content": txt})
        return out
