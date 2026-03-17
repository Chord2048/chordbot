from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from chordcode.agents.registry import AgentRegistry, agent_registry
from chordcode.agents.types import AgentDefinition, AgentLimits, RunRequest, RunResult
from chordcode.bus.bus import Bus, Event
from chordcode.config import Config
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker
from chordcode.llm.openai_chat import OpenAIChatProvider
from chordcode.log import logger
from chordcode.loop.interrupt import InterruptManager
from chordcode.loop.session_loop import SessionLoop
from chordcode.memory.service import MemoryService
from chordcode.model import Message, ModelRef, PermissionRule, Session, SessionRuntime, TextPart
from chordcode.permission.service import PermissionService
from chordcode.runtime import DaytonaManager
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.base import ToolResult
from chordcode.tools.registry import ToolRegistry

class AgentService:
    def __init__(
        self,
        *,
        cfg: Config,
        bus: Bus,
        store: SQLiteStore,
        perm: PermissionService,
        llm: OpenAIChatProvider,
        interrupt: InterruptManager,
        hooks: Hooker | None,
        memory_service: MemoryService,
        daytona_manager: DaytonaManager,
        mcp_manager: Any,
        kb_client: Any = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._store = store
        self._perm = perm
        self._llm = llm
        self._interrupt = interrupt
        self._hooks = hooks
        self._memory_service = memory_service
        self._daytona_manager = daytona_manager
        self._mcp_manager = mcp_manager
        self._kb_client = kb_client
        self._registry = registry or agent_registry
        self._log = logger.child(service="agent.service")
        self._active_runs: set[str] = set()
        self._active_runs_lock = asyncio.Lock()

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    def get_agent(self, name: str) -> AgentDefinition:
        return self._registry.get(name)

    async def add_user_message(self, session: Session, text: str, *, source: str = "api") -> str:
        if not text.strip():
            raise ValueError("text is required")
        now = int(time.time() * 1000)
        msg = Message(
            id=str(uuid4()),
            session_id=session.id,
            role="user",
            parent_id=None,
            agent=session.agent_name,
            model=ModelRef(provider="openai-compatible", id=self._cfg.openai.model),
            created_at=now,
        )
        out: dict[str, object] = {"text": text}
        if self._hooks:
            await self._hooks.trigger(Hook.ChatMessage, {"session_id": session.id, "agent": msg.agent, "message_id": msg.id}, out)
        text = str(out.get("text") or text)
        await self._store.add_message(msg)
        text_part = TextPart(id=str(uuid4()), message_id=msg.id, session_id=session.id, text=text)
        await self._store.add_part(session.id, msg.id, text_part)
        await self._store.touch_session(session.id)
        await self._bus.publish(Event(type="message.updated", properties={"session_id": session.id, "info": msg.model_dump()}))
        await self._bus.publish(
            Event(
                type="message.part.updated",
                properties={"session_id": session.id, "message_id": msg.id, "part": text_part.model_dump(), "delta": text},
            ),
        )
        self._log.info(
            "User message stored",
            event="message.user.added",
            session_id=session.id,
            message_id=msg.id,
            source=source,
            agent=session.agent_name,
            content_chars=len(text),
        )
        return msg.id

    async def create_child_session(
        self,
        *,
        parent_session: Session,
        agent_name: str,
        description: str,
        parent_tool_call_id: str,
    ) -> Session:
        now = int(time.time() * 1000)
        session = Session(
            id=str(uuid4()),
            title=f"[{agent_name}] {description}",
            worktree=parent_session.worktree,
            cwd=parent_session.cwd,
            created_at=now,
            updated_at=now,
            permission_rules=self._build_child_permission_rules(parent_session.permission_rules, agent_name),
            runtime=SessionRuntime.model_validate(parent_session.runtime.model_dump()),
            kind="subagent",
            agent_name=agent_name,
            root_session_id=parent_session.root_session_id or parent_session.id,
            parent_session_id=parent_session.id,
            parent_tool_call_id=parent_tool_call_id,
        )
        await self._store.create_session(session)
        if session.runtime.backend == "local":
            await self._memory_service.ensure_worktree(session.worktree)
        await self._bus.publish(Event(type="session.created", properties={"session_id": session.id, "info": session.model_dump()}))
        return session

    async def resolve_child_session(self, *, parent_session: Session, session_id: str, agent_name: str) -> Session:
        session = await self._store.get_session(session_id)
        root_session_id = parent_session.root_session_id or parent_session.id
        if session.kind != "subagent":
            raise ValueError(f"session is not a subagent session: {session_id}")
        if session.agent_name != agent_name:
            raise ValueError(f"subagent session agent mismatch: expected {agent_name}, got {session.agent_name}")
        if (session.root_session_id or session.id) != root_session_id:
            raise ValueError("subagent session belongs to a different root session")
        if await self.is_session_busy(session.id):
            raise ValueError(f"subagent session is currently busy: {session.id}")
        return session

    async def extract_assistant_text(self, session_id: str, message_id: str) -> str:
        history = await self._store.list_messages(session_id)
        target = next((m for m in history if m.info.id == message_id), None)
        if not target:
            return ""
        chunks = [getattr(part, "text", "") for part in target.parts if getattr(part, "type", None) == "text"]
        return "".join(chunks).strip()

    async def run(self, request: RunRequest, *, tools: ToolRegistry | None = None) -> RunResult:
        await self._mark_session_busy(request.session_id)
        try:
            session = await self._store.get_session(request.session_id)
            agent = self.get_agent(request.agent_name)
            tool_registry = tools or await self.build_tools(session, request.agent_name)
            loop = SessionLoop(
                cfg=self._cfg,
                bus=self._bus,
                store=self._store,
                perm=self._perm,
                tools=tool_registry,
                llm=self._llm,
                interrupt=self._interrupt,
                hooks=self._hooks,
                agent_definition=agent,
            )
            assistant_message_id, trace_id, finish = await loop.run(request=request)
            return RunResult(assistant_message_id=assistant_message_id, trace_id=trace_id, finish=finish)
        finally:
            await self._mark_session_idle(request.session_id)

    async def is_session_busy(self, session_id: str) -> bool:
        async with self._active_runs_lock:
            return session_id in self._active_runs

    async def _mark_session_busy(self, session_id: str) -> None:
        async with self._active_runs_lock:
            if session_id in self._active_runs:
                raise RuntimeError(f"session is already running: {session_id}")
            self._active_runs.add(session_id)

    async def _mark_session_idle(self, session_id: str) -> None:
        async with self._active_runs_lock:
            self._active_runs.discard(session_id)

    async def execute_task(
        self,
        *,
        parent_session: Session,
        description: str,
        prompt: str,
        subagent_type: str,
        resume_session_id: str | None,
        parent_ctx: Any,
    ) -> ToolResult:
        agent = self.get_agent(subagent_type)
        if agent.mode != "subagent":
            raise ValueError(f"agent is not a subagent: {subagent_type}")
        await parent_ctx.ask(
            permission="task",
            patterns=[subagent_type],
            always=[subagent_type],
            metadata={"description": description, "subagent_type": subagent_type},
        )

        if resume_session_id:
            child_session = await self.resolve_child_session(
                parent_session=parent_session,
                session_id=resume_session_id,
                agent_name=subagent_type,
            )
        else:
            child_session = await self.create_child_session(
                parent_session=parent_session,
                agent_name=subagent_type,
                description=description,
                parent_tool_call_id=parent_ctx.tool_part_id,
            )

        await self._bus.publish(
            Event(
                type="task.started",
                properties={
                    "session_id": parent_session.id,
                    "child_session_id": child_session.id,
                    "subagent_type": subagent_type,
                    "call_id": parent_ctx.tool_part_id,
                    "description": description,
                    "parallel_group_id": getattr(parent_ctx, "parallel_group_id", None),
                    "parallel_index": getattr(parent_ctx, "parallel_index", None),
                    "parallel_size": getattr(parent_ctx, "parallel_size", None),
                },
            ),
        )

        await self.add_user_message(child_session, prompt, source=f"task:{parent_ctx.tool_part_id}")
        request = RunRequest(
            session_id=child_session.id,
            agent_name=subagent_type,
            source=f"task:{parent_ctx.tool_part_id}",
            root_session_id=child_session.root_session_id or child_session.id,
            parent_session_id=parent_session.id,
            parent_tool_call_id=parent_ctx.tool_part_id,
            trace_id=getattr(parent_ctx, "trace_id", None),
            parent_observation_id=getattr(parent_ctx, "parent_observation_id", None),
            limits=agent.limits,
        )

        started = time.monotonic()
        child_task = asyncio.create_task(self.run(request))
        timed_out = False
        interrupted = False
        while not child_task.done():
            done, _pending = await asyncio.wait({child_task}, timeout=0.25)
            if done:
                break
            if await self._interrupt.is_interrupted(parent_session.id):
                interrupted = True
                await self._interrupt.interrupt(child_session.id, reason="parent_cancelled")
                child_task.cancel()
                break
            max_wall_time_ms = agent.limits.max_wall_time_ms or 0
            if max_wall_time_ms > 0 and (time.monotonic() - started) * 1000 >= max_wall_time_ms:
                timed_out = True
                await self._interrupt.interrupt(child_session.id, reason="timed_out")
                child_task.cancel()
                break

        result: RunResult | None = None
        error_code: str | None = None
        error_message: str | None = None
        try:
            result = await child_task
        except asyncio.CancelledError:
            error_code = "timed_out" if timed_out else "interrupted"
        except Exception as exc:
            error_code = "run_failed"
            error_message = str(exc)

        status = "completed"
        assistant_message_id: str | None = None
        trace_id: str | None = None
        if result is not None:
            assistant_message_id = result.assistant_message_id
            trace_id = result.trace_id
            if result.finish:
                status = result.finish
        elif timed_out:
            status = "timed_out"
        elif interrupted:
            status = "interrupted"
        else:
            status = "error"

        text = ""
        if assistant_message_id:
            text = await self.extract_assistant_text(child_session.id, assistant_message_id)
        if not text:
            if status == "timed_out":
                text = "Subagent timed out before producing a final summary."
            elif status == "interrupted":
                text = "Subagent was interrupted before producing a final summary."
            elif error_message:
                text = f"Subagent failed: {error_message}"
            else:
                text = "Subagent returned no summary."

        metadata: dict[str, Any] = {
            "session_id": child_session.id,
            "subagent_type": subagent_type,
            "assistant_message_id": assistant_message_id,
            "trace_id": trace_id,
            "status": status,
        }
        if error_code:
            metadata["error_code"] = error_code
        if error_message:
            metadata["error_message"] = error_message[:500]
        if getattr(parent_ctx, "parallel_group_id", None):
            metadata["parallel_group_id"] = parent_ctx.parallel_group_id

        output = self._format_task_output(text=text, metadata=metadata)
        event_type = "task.finished" if status == "completed" else "task.failed"
        await self._bus.publish(
            Event(
                type=event_type,
                properties={
                    "session_id": parent_session.id,
                    "child_session_id": child_session.id,
                    "subagent_type": subagent_type,
                    "call_id": parent_ctx.tool_part_id,
                    "description": description,
                    "trace_id": trace_id,
                    "status": status,
                    "parallel_group_id": getattr(parent_ctx, "parallel_group_id", None),
                    "parallel_index": getattr(parent_ctx, "parallel_index", None),
                    "parallel_size": getattr(parent_ctx, "parallel_size", None),
                },
            ),
        )
        self._log.info(
            "Task finished",
            event=(
                "task.delegate.finish"
                if status == "completed"
                else "task.delegate.timeout"
                if status == "timed_out"
                else "task.delegate.interrupted"
                if status == "interrupted"
                else "task.delegate.error"
            ),
            session_id=parent_session.id,
            parent_session_id=parent_session.id,
            root_session_id=parent_session.root_session_id or parent_session.id,
            tool_call_id=parent_ctx.tool_part_id,
            agent=subagent_type,
            child_session_id=child_session.id,
            trace_id=trace_id,
            status=status,
            parallel_group_id=getattr(parent_ctx, "parallel_group_id", None),
            parallel_index=getattr(parent_ctx, "parallel_index", None),
            parallel_size=getattr(parent_ctx, "parallel_size", None),
        )
        return ToolResult(title=description, output=output, metadata=metadata)

    async def build_tools(self, session: Session, agent_name: str) -> ToolRegistry:
        from chordcode.tools.bash import BashCtx, BashTool
        from chordcode.tools.daytona import DaytonaBashTool, DaytonaCtx, DaytonaGlobTool, DaytonaGrepTool, DaytonaReadTool, DaytonaWriteTool
        from chordcode.tools.files import FileCtx, ReadTool, WriteTool
        from chordcode.tools.grep import GlobTool, GrepTool, SearchCtx
        from chordcode.tools.kb_search import KBSearchCtx, KBSearchTool
        from chordcode.tools.memory import MemoryGetTool, MemorySearchTool, MemoryToolCtx
        from chordcode.tools.skill import SkillCtx, SkillTool
        from chordcode.tools.task import TaskTool
        from chordcode.tools.todo import TodoWriteTool
        from chordcode.tools.web import TavilySearchTool, WebFetchTool, WebSearchCtx
        from chordcode.mcp import MCPToolAdapter

        agent = self.get_agent(agent_name)
        runtime_tools: list[Any]
        if session.runtime.backend == "daytona":
            sandbox_ref = await self._daytona_manager.get_sandbox_for_session(session)
            daytona_ctx = DaytonaCtx(
                worktree=session.worktree,
                cwd=session.cwd,
                sandbox=sandbox_ref.sandbox,
                sandbox_id=sandbox_ref.sandbox_id,
                manager=self._daytona_manager,
            )
            runtime_tools = [
                DaytonaBashTool(daytona_ctx),
                DaytonaReadTool(daytona_ctx),
                DaytonaGlobTool(daytona_ctx),
                DaytonaGrepTool(daytona_ctx),
                DaytonaWriteTool(daytona_ctx),
            ]
        else:
            runtime_tools = [
                BashTool(BashCtx(worktree=session.worktree, cwd=session.cwd)),
                ReadTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
                GlobTool(SearchCtx(worktree=session.worktree, cwd=session.cwd)),
                GrepTool(SearchCtx(worktree=session.worktree, cwd=session.cwd)),
                WriteTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
            ]

        builtin_tools = runtime_tools + [
            TavilySearchTool(WebSearchCtx(tavily_api_key=self._cfg.web_search.tavily_api_key)),
            WebFetchTool(),
            SkillTool(SkillCtx(worktree=session.worktree, cwd=session.cwd, permission_rules=session.permission_rules)),
            TodoWriteTool(store=self._store, bus=self._bus),
        ]

        if agent.mode == "primary":
            builtin_tools.append(TaskTool(service=self, parent_session=session))

        if self._kb_client is not None:
            builtin_tools.append(KBSearchTool(KBSearchCtx(kb=self._kb_client)))

        if session.runtime.backend == "local" and self._memory_service.enabled:
            manager = await self._memory_service.get_manager(session.worktree)
            if manager is not None:
                memory_ctx = MemoryToolCtx(manager=manager)
                builtin_tools.extend([MemorySearchTool(memory_ctx), MemoryGetTool(memory_ctx)])

        mcp_tool_infos = await self._mcp_manager.list_tools()
        builtin_tools.extend([MCPToolAdapter(info, self._mcp_manager) for info in mcp_tool_infos])

        if agent.tool_allowlist is not None:
            allow = set(agent.tool_allowlist)
            builtin_tools = [tool for tool in builtin_tools if getattr(tool, "name", "") in allow]
        return ToolRegistry(builtin_tools)

    def _build_child_permission_rules(self, parent_rules: list[PermissionRule], agent_name: str) -> list[PermissionRule]:
        agent = self.get_agent(agent_name)
        relevant = {
            "read",
            "glob",
            "grep",
            "memory_search",
            "memory_get",
            "websearch",
            "webfetch",
            "external_directory",
            "task",
            "*",
        }
        inherited_denies = [
            rule for rule in parent_rules if rule.action == "deny" and rule.permission in relevant
        ]
        return [*inherited_denies, *agent.permission_profile]

    def _format_task_output(self, *, text: str, metadata: dict[str, Any]) -> str:
        lines = [
            "<subagent_summary>",
            text.strip(),
            "</subagent_summary>",
            "",
            "<task_metadata>",
        ]
        for key in (
            "session_id",
            "subagent_type",
            "assistant_message_id",
            "trace_id",
            "status",
            "error_code",
            "error_message",
            "parallel_group_id",
        ):
            value = metadata.get(key)
            if value in (None, ""):
                continue
            lines.append(f"{key}: {value}")
        lines.append("</task_metadata>")
        return "\n".join(lines)
