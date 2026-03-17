from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.agents.service import AgentService
from chordcode.agents.types import RunResult
from chordcode.bus.bus import Bus
from chordcode.config import (
    ChannelsConfig,
    Config,
    FeishuChannelConfig,
    HooksConfig,
    KBConfig,
    LangfuseConfig,
    LoggingConfig,
    MemoryConfig,
    OpenAIConfig,
    VLMConfig,
    WebSearchConfig,
)
from chordcode.hooks import Hooker
from chordcode.llm.openai_chat import Finish, ToolCall
from chordcode.loop.interrupt import InterruptManager
from chordcode.loop.session_loop import SessionLoop
from chordcode.memory.service import MemoryService
from chordcode.model import Message, ModelRef, PermissionRule, ReasoningPart, Session, TextPart
from chordcode.permission.service import PermissionService
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.base import ToolResult
from chordcode.tools.registry import ToolRegistry


def _test_config(db_path: str, worktree: str) -> Config:
    return Config(
        openai=OpenAIConfig(base_url="http://local", api_key="k", model="m"),
        langfuse=LangfuseConfig(
            enabled=False,
            public_key="",
            secret_key="",
            base_url="https://cloud.langfuse.com",
            environment="test",
            sample_rate=1.0,
            debug=False,
        ),
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=False,
                app_id="",
                app_secret="",
                encrypt_key="",
                verification_token="",
                allow_from=[],
            )
        ),
        kb=KBConfig(backend="none", base_url="", api_key=""),
        vlm=VLMConfig(backend="none", api_url="", api_key="", poll_interval=5, timeout=1800),
        logging=LoggingConfig(level="INFO", console=True, file=False, dir="./data/logs", rotation="00:00", retention="7 days"),
        hooks=HooksConfig(debug=False),
        web_search=WebSearchConfig(tavily_api_key=""),
        system_prompt="sys",
        db_path=db_path,
        default_worktree=worktree,
        default_permission_action="ask",
        prompt_templates={},
        memory=MemoryConfig(enabled=False),
    )


class FakeParallelTaskLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, *, system: str, messages: list[dict], tools: list[dict], params=None, headers=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(
                type="tool_call",
                call_id="call1",
                name="task",
                args_json=json.dumps({"label": "first", "delay_ms": 80}),
            )
            yield ToolCall(
                type="tool_call",
                call_id="call2",
                name="task",
                args_json=json.dumps({"label": "second", "delay_ms": 10}),
            )
            yield Finish(type="finish", reason="tool_calls")
            return
        yield Finish(type="finish", reason="stop")


class ParallelAwareTaskTool:
    name = "task"
    description = "fake task"

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self._started = 0
        self._active = 0
        self.max_active = 0

    def schema(self):
        return {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "delay_ms": {"type": "integer"},
            },
            "required": ["label", "delay_ms"],
        }

    async def execute(self, args, ctx):
        self._started += 1
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        if self._started >= 2:
            self._gate.set()
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=0.5)
            await asyncio.sleep(float(args["delay_ms"]) / 1000.0)
            return ToolResult(
                title=str(args["label"]),
                output=f"done:{args['label']}",
                metadata={
                    "parallel_group_id": ctx.parallel_group_id,
                    "parallel_index": ctx.parallel_index,
                    "parallel_size": ctx.parallel_size,
                },
            )
        finally:
            self._active -= 1


class FakeParentCtx:
    def __init__(self) -> None:
        self.tool_part_id = "call-parent"
        self.trace_id = "trace-root"
        self.parent_observation_id = "obs-parent"
        self.parallel_group_id = None
        self.parallel_index = None
        self.parallel_size = None
        self.asked: list[dict] = []

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict) -> None:
        self.asked.append(
            {
                "permission": permission,
                "patterns": patterns,
                "always": always,
                "metadata": metadata,
            }
        )


class FakeMCPManager:
    async def list_tools(self):
        return []


class SubagentTaskingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_task_batch_executes_concurrently_and_writes_results_in_call_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            session = Session(
                id="s1",
                title="Parallel Parent",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
            )
            await store.create_session(session)

            user = Message(
                id="u1",
                session_id=session.id,
                role="user",
                parent_id=None,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="m"),
                created_at=1,
            )
            await store.add_message(user)
            await store.add_part(session.id, user.id, TextPart(id="p1", message_id=user.id, session_id=session.id, text="run tasks"))

            cfg = _test_config(db, tmp)
            bus = Bus()
            hooks = Hooker()
            perm = PermissionService(bus, store, hooks)
            tool = ParallelAwareTaskTool()
            loop = SessionLoop(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                tools=ToolRegistry([tool]),
                llm=FakeParallelTaskLLM(),
                interrupt=InterruptManager(),
                hooks=hooks,
            )

            await loop.run(session_id=session.id)

            self.assertGreaterEqual(tool.max_active, 2)

            history = await store.list_messages(session.id)
            tool_messages = [m for m in history if m.info.role == "tool"]
            self.assertEqual([m.info.tool_call_id for m in tool_messages], ["call1", "call2"])
            tool_outputs = ["".join([p.text for p in m.parts if getattr(p, "type", "") == "text"]) for m in tool_messages]
            self.assertEqual(tool_outputs, ["done:first", "done:second"])

            assistant = next(m for m in history if m.info.role == "assistant")
            completed_tool_parts = [
                part for part in assistant.parts if getattr(part, "type", "") == "tool" and getattr(part.state, "status", "") == "completed"
            ]
            self.assertEqual(len(completed_tool_parts), 2)
            group_ids = [part.state.metadata.get("parallel_group_id") for part in completed_tool_parts]
            self.assertTrue(group_ids[0])
            self.assertEqual(group_ids[0], group_ids[1])
            self.assertEqual(
                [part.state.metadata.get("parallel_index") for part in completed_tool_parts],
                [1, 2],
            )
            self.assertEqual(
                [part.state.metadata.get("parallel_size") for part in completed_tool_parts],
                [2, 2],
            )

    async def test_primary_sessions_expose_task_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            cfg = _test_config(db, tmp)
            bus = Bus()
            hooks = Hooker()
            perm = PermissionService(bus, store, hooks)
            memory_service = MemoryService(cfg=cfg, store=store)

            parent = Session(
                id="parent",
                title="Parent",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                kind="primary",
                agent_name="primary",
                root_session_id="parent",
            )
            await store.create_session(parent)

            service = AgentService(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                llm=SimpleNamespace(),
                interrupt=InterruptManager(),
                hooks=hooks,
                memory_service=memory_service,
                daytona_manager=SimpleNamespace(),
                mcp_manager=FakeMCPManager(),
                kb_client=None,
            )

            tools = await service.build_tools(parent, parent.agent_name)
            self.assertEqual(tools.get("task").name, "task")

    async def test_explore_subagent_includes_web_tools_and_read_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            cfg = _test_config(db, tmp)
            bus = Bus()
            hooks = Hooker()
            perm = PermissionService(bus, store, hooks)
            memory_service = MemoryService(cfg=cfg, store=store)

            parent = Session(
                id="parent",
                title="Parent",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[
                    PermissionRule(permission="webfetch", pattern="blocked.example/*", action="deny"),
                    PermissionRule(permission="*", pattern="*", action="allow"),
                ],
                kind="primary",
                agent_name="primary",
                root_session_id="parent",
            )
            await store.create_session(parent)

            service = AgentService(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                llm=SimpleNamespace(),
                interrupt=InterruptManager(),
                hooks=hooks,
                memory_service=memory_service,
                daytona_manager=SimpleNamespace(),
                mcp_manager=FakeMCPManager(),
                kb_client=None,
            )

            child = await service.create_child_session(
                parent_session=parent,
                agent_name="explore",
                description="Inspect docs",
                parent_tool_call_id="call-parent",
            )
            tools = await service.build_tools(child, "explore")

            self.assertEqual(tools.get("websearch").name, "websearch")
            self.assertEqual(tools.get("webfetch").name, "webfetch")
            with self.assertRaises(KeyError):
                tools.get("bash")
            with self.assertRaises(KeyError):
                tools.get("write")
            with self.assertRaises(KeyError):
                tools.get("task")

            rule_map = {(rule.permission, rule.pattern): rule.action for rule in child.permission_rules}
            self.assertEqual(rule_map[("websearch", "*")], "allow")
            self.assertEqual(rule_map[("webfetch", "*")], "allow")
            self.assertEqual(rule_map[("webfetch", "blocked.example/*")], "deny")

    async def test_execute_task_uses_child_final_text_and_hides_child_sessions_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            cfg = _test_config(db, tmp)
            bus = Bus()
            hooks = Hooker()
            perm = PermissionService(bus, store, hooks)
            memory_service = MemoryService(cfg=cfg, store=store)

            parent = Session(
                id="parent",
                title="Parent",
                worktree=tmp,
                cwd=tmp,
                created_at=1,
                updated_at=1,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                kind="primary",
                agent_name="primary",
                root_session_id="parent",
            )
            await store.create_session(parent)

            service = AgentService(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                llm=SimpleNamespace(),
                interrupt=InterruptManager(),
                hooks=hooks,
                memory_service=memory_service,
                daytona_manager=SimpleNamespace(),
                mcp_manager=FakeMCPManager(),
                kb_client=None,
            )

            async def fake_run(request, *, tools=None):
                history = await store.list_messages(request.session_id)
                user = next(m for m in reversed(history) if m.info.role == "user")
                assistant_id = f"assistant-{request.session_id}"
                assistant = Message(
                    id=assistant_id,
                    session_id=request.session_id,
                    role="assistant",
                    parent_id=user.info.id,
                    agent=request.agent_name,
                    model=ModelRef(provider="openai-compatible", id="m"),
                    created_at=int(time.time() * 1000),
                )
                await store.add_message(assistant)
                await store.add_part(
                    request.session_id,
                    assistant_id,
                    TextPart(id=f"text-{request.session_id}", message_id=assistant_id, session_id=request.session_id, text="final summary"),
                )
                await store.add_part(
                    request.session_id,
                    assistant_id,
                    ReasoningPart(
                        id=f"reason-{request.session_id}",
                        message_id=assistant_id,
                        session_id=request.session_id,
                        text="hidden reasoning",
                        time={"start": 1, "end": 2},
                    ),
                )
                tool_msg = Message(
                    id=f"tool-{request.session_id}",
                    session_id=request.session_id,
                    role="tool",
                    parent_id=assistant_id,
                    agent=request.agent_name,
                    model=ModelRef(provider="openai-compatible", id="m"),
                    created_at=int(time.time() * 1000),
                    tool_call_id="child-tool",
                    tool_name="read",
                )
                await store.add_message(tool_msg)
                await store.add_part(
                    request.session_id,
                    tool_msg.id,
                    TextPart(id=f"tool-text-{request.session_id}", message_id=tool_msg.id, session_id=request.session_id, text="hidden tool output", synthetic=True),
                )
                await store.update_message(assistant_id, completed_at=int(time.time() * 1000), finish="completed")
                return RunResult(assistant_message_id=assistant_id, trace_id="trace-child", finish="completed")

            service.run = fake_run  # type: ignore[method-assign]

            ctx = FakeParentCtx()
            result = await service.execute_task(
                parent_session=parent,
                description="Explore code",
                prompt="Inspect the repository",
                subagent_type="explore",
                resume_session_id=None,
                parent_ctx=ctx,
            )

            self.assertEqual(result.title, "Explore code")
            self.assertIn("<subagent_summary>\nfinal summary\n</subagent_summary>", result.output)
            self.assertNotIn("hidden reasoning", result.output)
            self.assertNotIn("hidden tool output", result.output)
            self.assertIn("subagent_type: explore", result.output)
            self.assertIn("status: completed", result.output)
            self.assertIn("trace_id: trace-child", result.output)
            self.assertEqual(ctx.asked[0]["permission"], "task")
            self.assertEqual(ctx.asked[0]["patterns"], ["explore"])

            child_session_id = str(result.metadata["session_id"])
            child_session = await store.get_session(child_session_id)
            self.assertEqual(child_session.kind, "subagent")
            self.assertEqual(child_session.agent_name, "explore")
            self.assertEqual(child_session.root_session_id, parent.id)
            self.assertEqual(child_session.parent_session_id, parent.id)
            self.assertEqual(child_session.parent_tool_call_id, ctx.tool_part_id)

            visible_roots = await store.list_sessions(include_children=False)
            self.assertEqual([session.id for session in visible_roots], [parent.id])
            children = await store.list_sessions(parent_session_id=parent.id)
            self.assertEqual([session.id for session in children], [child_session_id])
