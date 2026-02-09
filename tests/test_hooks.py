from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.bus.bus import Bus
from chordcode.config import Config, LangfuseConfig, OpenAIConfig
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker
from chordcode.llm.openai_chat import Error as LLMError
from chordcode.llm.openai_chat import Finish, ToolCall
from chordcode.loop.interrupt import InterruptManager
from chordcode.loop.session_loop import SessionLoop
from chordcode.model import Message, ModelRef, PermissionRule, Session, TextPart
from chordcode.permission.service import PermissionService
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.base import ToolResult
from chordcode.tools.registry import ToolRegistry


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, *, system: str, messages: list[dict], tools: list[dict], params=None, headers=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(type="tool_call", call_id="call1", name="echo", args_json=json.dumps({"x": 1}))
            yield Finish(type="finish", reason="tool_calls")
            return
        yield Finish(type="finish", reason="stop")


class FakeLLMError:
    async def stream(self, *, system: str, messages: list[dict], tools: list[dict], params=None, headers=None, **_kwargs):
        yield LLMError(type="error", message="provider unavailable")


class EchoTool:
    name = "echo"
    description = "echo"

    def schema(self):
        return {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}

    async def execute(self, args, ctx):
        return ToolResult(title="echo", output=str(args.get("x")), metadata={})


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_hooker_orders_and_mutates(self):
        hooks = Hooker()

        async def a(_input, output):
            output["x"] = int(output.get("x") or 0) + 1

        async def b(_input, output):
            output["x"] = int(output.get("x") or 0) * 2

        hooks.add({"test": a})
        hooks.add({"test": b})

        out = await hooks.trigger("test", {}, {"x": 1})
        self.assertEqual(out["x"], 4)

    async def test_wildcard_runs_before_and_after(self):
        hooks = Hooker()
        seen: list[tuple[str, str]] = []

        async def all(input, _output):
            hook = str(input.get("hook") or "")
            phase = str(input.get("phase") or "")
            seen.append((hook, phase))

        async def run(_input, output):
            output["y"] = 1

        hooks.add({"*": all})
        hooks.add({"x": run})

        await hooks.trigger("x", {"a": 1}, {})
        self.assertEqual(seen[0], ("x", "before"))
        self.assertEqual(seen[-1], ("x", "after"))

    async def test_tool_hooks_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            sid = "s1"
            now = 1
            session = Session(
                id=sid,
                title="t",
                worktree="/tmp",
                cwd="/tmp",
                created_at=now,
                updated_at=now,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
            )
            await store.create_session(session)

            user = Message(
                id="u1",
                session_id=sid,
                role="user",
                parent_id=None,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="x"),
                created_at=now,
            )
            await store.add_message(user)
            await store.add_part(
                sid,
                user.id,
                TextPart(id="p1", message_id=user.id, session_id=sid, text="hi"),
            )

            hooks = Hooker()

            async def before(_input, output):
                args = output.get("args")
                if isinstance(args, dict):
                    args["x"] = 7

            async def after(_input, output):
                output["output"] = "ok"

            hooks.add({Hook.ToolExecuteBefore: before, Hook.ToolExecuteAfter: after})

            bus = Bus()
            perm = PermissionService(bus, store, hooks)
            tools = ToolRegistry([EchoTool()])
            cfg = Config(
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
                system_prompt="sys",
                db_path=db,
                default_worktree="/tmp",
                default_permission_action="ask",
            )
            loop = SessionLoop(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                tools=tools,
                llm=FakeLLM(),
                interrupt=InterruptManager(),
                hooks=hooks,
            )

            await loop.run(session_id=sid)

            history = await store.list_messages(sid)
            tool = next(m for m in history if m.info.role == "tool")
            txt = "".join([p.text for p in tool.parts if getattr(p, "type", "") == "text"])
            self.assertEqual(txt, "ok")

    async def test_llm_error_event_marks_assistant_message_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            sid = "s1"
            now = 1
            session = Session(
                id=sid,
                title="t",
                worktree="/tmp",
                cwd="/tmp",
                created_at=now,
                updated_at=now,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
            )
            await store.create_session(session)

            user = Message(
                id="u1",
                session_id=sid,
                role="user",
                parent_id=None,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="x"),
                created_at=now,
            )
            await store.add_message(user)
            await store.add_part(
                sid,
                user.id,
                TextPart(id="p1", message_id=user.id, session_id=sid, text="hi"),
            )

            bus = Bus()
            hooks = Hooker()
            perm = PermissionService(bus, store, hooks)
            tools = ToolRegistry([EchoTool()])
            cfg = Config(
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
                system_prompt="sys",
                db_path=db,
                default_worktree="/tmp",
                default_permission_action="ask",
            )
            loop = SessionLoop(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                tools=tools,
                llm=FakeLLMError(),
                interrupt=InterruptManager(),
                hooks=hooks,
            )

            assistant_id, _trace_id = await loop.run(session_id=sid)
            history = await store.list_messages(sid)
            assistant = next(m for m in history if m.info.id == assistant_id)
            self.assertEqual(assistant.info.finish, "error")
            self.assertIsNotNone(assistant.info.error)
            err = assistant.info.error or {}
            self.assertEqual(err.get("type"), "RuntimeError")
            self.assertIn("LLM provider error", str(err.get("message") or ""))
