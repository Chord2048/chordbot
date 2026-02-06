from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from chordcode.bus.bus import Bus, Event
from chordcode.config import Config
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker
from chordcode.llm.openai_chat import Finish, OpenAIChatProvider, TextDelta, ToolCall
from chordcode.loop.interrupt import InterruptManager
from chordcode.model import (
    Message,
    MessageWithParts,
    ModelRef,
    PermissionRule,
    TextPart,
    ToolPart,
    ToolStateCompleted,
    ToolStatePending,
    ToolStateRunning,
)
from chordcode.observability.langfuse_client import get_langfuse
from chordcode.permission.service import PermissionRejected, PermissionService
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.registry import ToolRegistry
from chordcode.log import log, log_context


@dataclass(frozen=True)
class ToolCtx:
    session_id: str
    message_id: str
    agent: str
    bus: Bus
    store: SQLiteStore
    perm: PermissionService
    ruleset: list[PermissionRule]
    tool_part_id: str

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict[str, Any]) -> None:
        await self.perm.ask(
            session_id=self.session_id,
            ruleset=self.ruleset,
            permission=permission,
            patterns=patterns,
            always=always,
            metadata=metadata,
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
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._perm = perm
        self._tools = tools
        self._llm = llm
        self._interrupt = interrupt
        self._hooks = hooks
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        l = self._locks.get(session_id)
        if l:
            return l
        l = asyncio.Lock()
        self._locks[session_id] = l
        return l

    async def run(self, *, session_id: str) -> tuple[str, str | None]:
        with log_context(session_id=session_id, event="session.run"):
            log.debug("Session run requested")
        async with self._lock(session_id):
            # Clear any previous interrupt
            await self._interrupt.clear(session_id)

        # Initialize Langfuse trace for this session using SDK v3 API
        langfuse = get_langfuse()
        
        # If Langfuse is enabled, wrap everything in a trace context
        if langfuse:
            try:
                # In SDK v3, create a span using context manager which implicitly creates the trace
                with langfuse.start_as_current_observation(
                    as_type="span",
                    name="agent-session",
                    metadata={
                        "agent": "primary",
                        "session_id": session_id,
                    }
                ) as trace_span:
                    trace_id = trace_span.trace_id
                    with log_context(trace_id=trace_id):
                        log.bind(event="langfuse.trace.start").debug("Langfuse trace started")
                    return await self._run_session_with_trace(session_id, trace_span, trace_id)
            except Exception as e:
                log.bind(event="langfuse.trace.start.error").opt(exception=e).error("Error creating Langfuse trace span")
                # Fall back to running without Langfuse
                return await self._run_session_with_trace(session_id, None, None)
        else:
            # No Langfuse, run without tracing
            return await self._run_session_with_trace(session_id, None, None)

    async def _run_session_with_trace(
        self, session_id: str, trace_span: Any | None, trace_id: str | None
    ) -> tuple[str, str | None]:
        """Helper method that contains the actual session loop logic."""
        try:
            session = await self._store.get_session(session_id)
            with log_context(
                session_id=session_id,
                trace_id=trace_id,
                agent="primary",
                event="session.start",
            ):
                log.bind(worktree=session.worktree, cwd=session.cwd, model=self._cfg.openai.model).info("Session started")
            
            # Update trace with session metadata
            if trace_span:
                try:
                    trace_span.update(
                        metadata={
                            "agent": "primary",
                            "worktree": session.worktree,
                            "cwd": session.cwd,
                            "model": self._cfg.openai.model,
                        }
                    )
                except Exception as e:
                    log.bind(event="langfuse.trace.update.error").opt(exception=e).error("Error updating Langfuse trace metadata")
            
            await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "busy"}))

            history = await self._store.list_messages(session_id)
            user = next((m for m in reversed(history) if m.info.role == "user"), None)
            if not user:
                raise RuntimeError("no user message")
        except Exception as e:
            # Record error in Langfuse trace
            if trace_span:
                try:
                    trace_span.update(
                        level="ERROR",
                        metadata={
                            "error_type": type(e).__name__,
                            "error_message": str(e),
                        }
                    )
                except Exception as err:
                    log.bind(event="langfuse.trace.error_update.error").opt(exception=err).error(
                        "Error updating Langfuse trace with error metadata",
                    )
            
            log.bind(event="session.error", session_id=session_id, trace_id=trace_id).opt(exception=e).error("Session failed early")
            await self._bus.publish(
                Event(
                    type="session.error",
                    properties={"session_id": session_id, "error": {"message": str(e), "type": type(e).__name__}},
                ),
            )
            await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))
            raise

        agent = "primary"
        model = ModelRef(provider="openai-compatible", id=self._cfg.openai.model)
        assistant_id = str(uuid4())

        assistant = Message(
            id=assistant_id,
            session_id=session_id,
            role="assistant",
            parent_id=user.info.id,
            agent=agent,
            model=model,
            created_at=int(time.time() * 1000),
        )
        await self._store.add_message(assistant)
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": assistant.model_dump()}))

        tools = self._tools.list()
        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.schema,
                },
            }
            for t in tools
        ]

        messages = self._to_openai_messages(history)

        while True:
            # Check for interruption
            if await self._interrupt.is_interrupted(session_id):
                signal = await self._interrupt.check(session_id)
                reason = signal.reason if signal else "interrupted"
                await self._store.update_message(assistant_id, completed_at=int(time.time() * 1000), finish=reason)
                await self._bus.publish(
                    Event(
                        type="message.updated",
                        properties={"session_id": session_id, "info": {**assistant.model_dump(), "finish": reason}},
                    ),
                )
                await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))
                return assistant_id, trace_id

            calls: list[ToolCall] = []
            reason: str | None = None

            # Track current text part for streaming
            current_text_part: TextPart | None = None
            current_text_part_id: str | None = None

            try:
                system = self._cfg.system_prompt
                if self._hooks:
                    sysout: dict[str, object] = {"system": [system]}
                    await self._hooks.trigger(
                        Hook.ExperimentalChatSystemTransform,
                        {"session_id": session_id, "agent": agent, "model": model.model_dump()},
                        sysout,
                    )
                    raw = sysout.get("system")
                    if isinstance(raw, list):
                        parts = [x for x in raw if isinstance(x, str)]
                        if parts:
                            system = "\n\n".join(parts)

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

                params: dict[str, object] = {"temperature": 1.0, "top_p": 0.95, "top_k": 0, "options": {}}
                headers: dict[str, object] = {"headers": {}}
                if self._hooks:
                    user_text = "".join([p.text for p in user.parts if getattr(p, "type", "") == "text"])
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
                hdrs = raw_headers if isinstance(raw_headers, dict) else {}

                llm_temperature = params.get("temperature")
                llm_top_p = params.get("top_p")
                messages_count = len(msgs)
                messages_chars = 0
                for m in msgs:
                    content = m.get("content")
                    if isinstance(content, str):
                        messages_chars += len(content)
                tools_count = len(tool_specs)
                system_chars = len(system)
                assistant_chars = 0

                with log_context(
                    session_id=session_id,
                    message_id=assistant_id,
                    trace_id=trace_id,
                    agent=agent,
                    event="llm.request",
                ):
                    log.bind(
                        system_chars=system_chars,
                        messages_count=messages_count,
                        messages_chars=messages_chars,
                        tools_count=tools_count,
                        temperature=llm_temperature,
                        top_p=llm_top_p,
                    ).debug("LLM request")

                async for evt in self._llm.stream(
                    system=system, 
                    messages=msgs, 
                    tools=tool_specs, 
                    params=params, 
                    headers=hdrs, 
                    langfuse_trace_id=trace_id,
                    langfuse_parent_observation_id=trace_span.id if trace_span else None
                ):
                    # Check interruption during streaming
                    if await self._interrupt.is_interrupted(session_id):
                        break
                    if isinstance(evt, TextDelta):
                        assistant_chars += len(evt.text)
                        # Create new text part if needed
                        if not current_text_part:
                            current_text_part_id = str(uuid4())
                            current_text_part = TextPart(
                                id=current_text_part_id,
                                message_id=assistant_id,
                                session_id=session_id,
                                text=evt.text,
                                time={"start": int(time.time() * 1000)},
                            )
                        else:
                            current_text_part.text += evt.text

                        # Publish delta event
                        await self._bus.publish(
                            Event(
                                type="message.part.updated",
                                properties={
                                    "session_id": session_id,
                                    "message_id": assistant_id,
                                    "part": current_text_part.model_dump(),
                                    "delta": evt.text,
                                },
                            ),
                        )
                        continue

                    if isinstance(evt, ToolCall):
                        calls.append(evt)
                        continue

                    if isinstance(evt, Finish):
                        reason = evt.reason
                        # Save final text part if exists
                        if current_text_part:
                            current_text_part.time = {
                                "start": current_text_part.time.get("start", 0),
                                "end": int(time.time() * 1000),
                            }
                            await self._store.add_part(session_id, assistant_id, current_text_part)
                            current_text_part = None
                        with log_context(
                            session_id=session_id,
                            message_id=assistant_id,
                            trace_id=trace_id,
                            agent=agent,
                            event="llm.finish",
                        ):
                            log.bind(finish_reason=reason, assistant_chars=assistant_chars).debug("LLM finished")
                        continue
            except Exception as e:
                log.bind(event="llm.stream.error", session_id=session_id, message_id=assistant_id, trace_id=trace_id).opt(exception=e).error(
                    "LLM streaming error",
                )
                # Handle streaming errors
                await self._store.update_message(
                    assistant_id,
                    completed_at=int(time.time() * 1000),
                    finish="error",
                    error={"message": str(e), "type": type(e).__name__},
                )
                await self._bus.publish(
                    Event(
                        type="session.error",
                        properties={
                            "session_id": session_id,
                            "error": {"message": str(e), "type": type(e).__name__, "context": "llm_stream"},
                        },
                    ),
                )
                await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))
                return assistant_id, trace_id

            if calls:
                for c in calls:
                    call_id = c.call_id
                    tool_name = c.name
                    raw = c.args_json

                    part_id = str(uuid4())
                    pending = ToolPart(
                        id=part_id,
                        message_id=assistant_id,
                        session_id=session_id,
                        call_id=call_id,
                        tool=tool_name,
                        state=ToolStatePending(input={}, raw=raw),
                    )
                    await self._store.add_part(session_id, assistant_id, pending)
                    await self._bus.publish(
                        Event(
                            type="message.part.updated",
                            properties={"session_id": session_id, "message_id": assistant_id, "part": pending.model_dump()},
                        ),
                    )

                    try:
                        args = json.loads(raw or "{}")
                    except Exception as e:
                        done = ToolPart(
                            id=part_id,
                            message_id=assistant_id,
                            session_id=session_id,
                            call_id=call_id,
                            tool=tool_name,
                            state=ToolStateCompleted(
                                input={},
                                title=tool_name,
                                output=f"Invalid tool arguments: {e}",
                                metadata={"error": True},
                                time={"start": int(time.time() * 1000), "end": int(time.time() * 1000)},
                            ),
                        )
                        await self._store.add_part(session_id, assistant_id, done)
                        await self._bus.publish(
                            Event(
                                type="message.part.updated",
                                properties={"session_id": session_id, "message_id": assistant_id, "part": done.model_dump()},
                            ),
                        )

                        tool_msg_id = str(uuid4())
                        tool_msg = Message(
                            id=tool_msg_id,
                            session_id=session_id,
                            role="tool",
                            parent_id=assistant_id,
                            agent=agent,
                            model=model,
                            created_at=int(time.time() * 1000),
                            tool_call_id=call_id,
                            tool_name=tool_name,
                        )
                        await self._store.add_message(tool_msg)
                        tool_text_part = TextPart(
                            id=str(uuid4()),
                            message_id=tool_msg_id,
                            session_id=session_id,
                            text=done.state.output,
                            synthetic=True,
                        )
                        await self._store.add_part(session_id, tool_msg_id, tool_text_part)
                        await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": tool_msg.model_dump()}))
                        continue

                    if self._hooks:
                        out: dict[str, object] = {"args": args}
                        await self._hooks.trigger(
                            Hook.ToolExecuteBefore,
                            {"tool": tool_name, "session_id": session_id, "call_id": call_id},
                            out,
                        )
                        raw_args = out.get("args")
                        if isinstance(raw_args, dict):
                            args = cast(dict[str, Any], raw_args)

                    start = int(time.time() * 1000)
                    with log_context(
                        session_id=session_id,
                        message_id=assistant_id,
                        trace_id=trace_id,
                        agent=agent,
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        event="tool.start",
                    ):
                        safe_args: dict[str, Any] = {}
                        if isinstance(args, dict):
                            safe_args["args_keys"] = list(args.keys())
                            safe_args["string_value_chars"] = {
                                k: len(v) for k, v in args.items() if isinstance(k, str) and isinstance(v, str)
                            }
                        log.bind(**safe_args).debug("Tool execution started")
                    running = ToolPart(
                        id=part_id,
                        message_id=assistant_id,
                        session_id=session_id,
                        call_id=call_id,
                        tool=tool_name,
                        state=ToolStateRunning(input=args, time={"start": start}),
                    )
                    await self._store.add_part(session_id, assistant_id, running)

                    t = self._tools.get(tool_name)
                    ctx = ToolCtx(
                        session_id=session_id,
                        message_id=assistant_id,
                        agent=agent,
                        bus=self._bus,
                        store=self._store,
                        perm=self._perm,
                        ruleset=session.permission_rules,
                        tool_part_id=call_id,
                    )

                    # Create context manager for tool execution span
                    if trace_span:
                        try:
                            tool_span_cm = trace_span.start_as_current_observation(
                                as_type="tool",
                                name=f"tool-{tool_name}",
                                input=args,
                                metadata={
                                    "tool": tool_name,
                                    "call_id": call_id,
                                },
                            )
                        except Exception as e:
                            log.bind(event="langfuse.tool_span.start.error", tool_name=tool_name, tool_call_id=call_id).opt(exception=e).error(
                                "Error creating Langfuse tool span",
                            )
                            tool_span_cm = nullcontext(None)
                    else:
                        tool_span_cm = nullcontext(None)

                    with tool_span_cm as tool_span:
                        try:
                            r = await t.execute(args, ctx)

                            # Update tool span with output
                            if tool_span:
                                try:
                                    tool_span.update(
                                        output=r.output,
                                        metadata={**r.metadata, "title": r.title},
                                    )
                                except Exception as e:
                                    log.bind(event="langfuse.tool_span.update.error", tool_name=tool_name, tool_call_id=call_id).opt(exception=e).error(
                                        "Error updating Langfuse tool span",
                                    )

                            out: dict[str, object] = {"title": r.title, "output": r.output, "metadata": r.metadata}
                            if self._hooks:
                                await self._hooks.trigger(
                                    Hook.ToolExecuteAfter,
                                    {"tool": tool_name, "session_id": session_id, "call_id": call_id},
                                    out,
                                )
                            title = str(out.get("title") or r.title)
                            output = str(out.get("output") or r.output)
                            raw_meta = out.get("metadata")
                            meta = raw_meta if isinstance(raw_meta, dict) else r.metadata
                            done = ToolPart(
                                id=part_id,
                                message_id=assistant_id,
                                session_id=session_id,
                                call_id=call_id,
                                tool=tool_name,
                                state=ToolStateCompleted(
                                    input=args,
                                    title=title,
                                    output=output,
                                    metadata=cast(dict[str, Any], meta),
                                    time={"start": start, "end": int(time.time() * 1000)},
                                ),
                            )
                            tool_output = output
                            end = int(time.time() * 1000)
                            with log_context(
                                session_id=session_id,
                                message_id=assistant_id,
                                trace_id=trace_id,
                                agent=agent,
                                tool_name=tool_name,
                                tool_call_id=call_id,
                                event="tool.finish",
                                duration_ms=float(end - start),
                            ):
                                log.bind(output_chars=len(tool_output)).debug("Tool execution finished")
                        except PermissionRejected as e:
                            # Update tool span with error
                            if tool_span:
                                try:
                                    tool_span.update(
                                        level="ERROR",
                                        metadata={
                                            "error": "PermissionRejected",
                                            "message": str(e),
                                        },
                                    )
                                except Exception as err:
                                    log.bind(event="langfuse.tool_span.error_update.error", tool_name=tool_name, tool_call_id=call_id).opt(exception=err).error(
                                        "Error updating Langfuse tool span with PermissionRejected",
                                    )

                            log.bind(event="tool.blocked", tool_name=tool_name, tool_call_id=call_id, session_id=session_id, message_id=assistant_id, trace_id=trace_id).warning(
                                "Tool blocked by permission",
                            )
                            await self._store.update_message(assistant_id, completed_at=int(time.time() * 1000), finish="blocked")
                            await self._bus.publish(
                                Event(
                                    type="message.updated",
                                    properties={"session_id": session_id, "info": {**assistant.model_dump(), "finish": "blocked"}},
                                ),
                            )
                            await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))
                            return assistant_id, trace_id
                        except Exception as e:
                            # Update tool span with error
                            if tool_span:
                                try:
                                    tool_span.update(
                                        level="ERROR",
                                        metadata={
                                            "error": type(e).__name__,
                                            "message": str(e),
                                        },
                                    )
                                except Exception as err:
                                    log.bind(event="langfuse.tool_span.error_update.error", tool_name=tool_name, tool_call_id=call_id).opt(exception=err).error(
                                        "Error updating Langfuse tool span with error",
                                    )
                            output = f"Tool execution failed: {e}"
                            out: dict[str, object] = {"title": tool_name, "output": output, "metadata": {"error": True}}
                            if self._hooks:
                                await self._hooks.trigger(
                                    Hook.ToolExecuteAfter,
                                    {"tool": tool_name, "session_id": session_id, "call_id": call_id},
                                    out,
                                )
                            title = str(out.get("title") or tool_name)
                            output = str(out.get("output") or output)
                            raw_meta = out.get("metadata")
                            meta = raw_meta if isinstance(raw_meta, dict) else {"error": True}
                            done = ToolPart(
                                id=part_id,
                                message_id=assistant_id,
                                session_id=session_id,
                                call_id=call_id,
                                tool=tool_name,
                                state=ToolStateCompleted(
                                    input=args,
                                    title=title,
                                    output=output,
                                    metadata=cast(dict[str, Any], meta),
                                    time={"start": start, "end": int(time.time() * 1000)},
                                ),
                            )
                            tool_output = output
                            end = int(time.time() * 1000)
                            with log_context(
                                session_id=session_id,
                                message_id=assistant_id,
                                trace_id=trace_id,
                                agent=agent,
                                tool_name=tool_name,
                                tool_call_id=call_id,
                                event="tool.error",
                                duration_ms=float(end - start),
                            ):
                                log.bind(output_chars=len(tool_output)).opt(exception=e).error("Tool execution failed")

                    await self._store.add_part(session_id, assistant_id, done)
                    await self._bus.publish(
                        Event(
                            type="message.part.updated",
                            properties={"session_id": session_id, "message_id": assistant_id, "part": done.model_dump()},
                        ),
                    )

                    tool_msg_id = str(uuid4())
                    tool_msg = Message(
                        id=tool_msg_id,
                        session_id=session_id,
                        role="tool",
                        parent_id=assistant_id,
                        agent=agent,
                        model=model,
                        created_at=int(time.time() * 1000),
                        tool_call_id=call_id,
                        tool_name=tool_name,
                    )
                    await self._store.add_message(tool_msg)
                    tool_text_part = TextPart(
                        id=str(uuid4()),
                        message_id=tool_msg_id,
                        session_id=session_id,
                        text=tool_output,
                        synthetic=True,
                    )
                    await self._store.add_part(session_id, tool_msg_id, tool_text_part)
                    await self._bus.publish(Event(type="message.updated", properties={"session_id": session_id, "info": tool_msg.model_dump()}))

                history = await self._store.list_messages(session_id)
                messages = self._to_openai_messages(history)
                continue

            if reason and reason != "tool_calls":
                await self._store.update_message(assistant_id, completed_at=int(time.time() * 1000), finish=reason)
                await self._bus.publish(
                    Event(
                        type="message.updated",
                        properties={"session_id": session_id, "info": {**assistant.model_dump(), "completed_at": int(time.time() * 1000), "finish": reason}},
                    ),
                )
                await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))
                return assistant_id, trace_id

        await self._bus.publish(Event(type="session.status", properties={"session_id": session_id, "status": "idle"}))
        return assistant_id, trace_id

    def _to_openai_messages(self, history: list[MessageWithParts]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in history:
            if m.info.role == "user":
                txt = "".join([p.text for p in m.parts if getattr(p, "type", "") == "text"])
                out.append({"role": "user", "content": txt})
                continue
            if m.info.role == "assistant":
                txt = "".join([p.text for p in m.parts if getattr(p, "type", "") == "text"])
                calls: dict[str, dict[str, Any]] = {}
                for p in m.parts:
                    if getattr(p, "type", "") != "tool":
                        continue
                    st = getattr(p, "state", None)
                    args_json = ""
                    if st and getattr(st, "status", "") == "pending":
                        args_json = str(getattr(st, "raw", "") or "")
                    if not args_json and st and getattr(st, "input", None) is not None:
                        args_json = json.dumps(getattr(st, "input"))
                    calls[p.call_id] = {
                        "id": p.call_id,
                        "type": "function",
                        "function": {"name": p.tool, "arguments": args_json},
                    }
                msg: dict[str, Any] = {"role": "assistant", "content": txt or ""}
                if calls:
                    msg["tool_calls"] = list(calls.values())
                out.append(msg)
                continue
            if m.info.role == "tool":
                txt = "".join([p.text for p in m.parts if getattr(p, "type", "") == "text"])
                out.append({"role": "tool", "tool_call_id": m.info.tool_call_id, "content": txt})
                continue
        return out
