from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.bus.bus import Bus
from chordcode.config import (
    ChannelsConfig,
    Config,
    FeishuChannelConfig,
    HooksConfig,
    KBConfig,
    LangfuseConfig,
    LoggingConfig,
    OpenAIConfig,
    VLMConfig,
    WebSearchConfig,
)
from chordcode.hooks import Hooker
from chordcode.llm.openai_chat import Finish, ReasoningDelta, ToolCall, _extract_reasoning_text
from chordcode.loop.interrupt import InterruptManager
from chordcode.loop.session_loop import SessionLoop
from chordcode.model import Message, ModelRef, PermissionRule, Session, TextPart
from chordcode.permission.service import PermissionService
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.base import ToolResult
from chordcode.tools.registry import ToolRegistry


class DeltaWithExtra:
    def __init__(self, payload):
        self.model_extra = payload


class ReasoningExtractTests(unittest.TestCase):
    def test_extract_reasoning_from_model_extra(self) -> None:
        delta = DeltaWithExtra(
            {"reasoning_content": [{"text": "first "}, {"content": [{"text": "second"}]}]}
        )
        self.assertEqual(_extract_reasoning_text(delta), "first second")


class FakeLLMWithReasoning:
    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[list[dict]] = []

    async def stream(self, *, system: str, messages: list[dict], tools: list[dict], params=None, headers=None, **_kwargs):
        self.calls += 1
        self.inputs.append(copy.deepcopy(messages))
        if self.calls == 1:
            yield ReasoningDelta(type="reasoning_delta", text="Need tool. ")
            yield ToolCall(type="tool_call", call_id="call1", name="echo", args_json=json.dumps({"x": 1}))
            yield Finish(type="finish", reason="tool_calls")
            return
        yield Finish(type="finish", reason="stop")


class EchoTool:
    name = "echo"
    description = "echo"

    def schema(self):
        return {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}

    async def execute(self, args, ctx):
        return ToolResult(title="echo", output=str(args.get("x")), metadata={})


class ReasoningToolCallHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_content_is_kept_on_assistant_tool_call_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            sid = "s1"
            now = 1
            session = Session(
                id=sid,
                title="t",
                worktree=tmp,
                cwd=tmp,
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
            await store.add_part(sid, user.id, TextPart(id="p1", message_id=user.id, session_id=sid, text="hi"))

            llm = FakeLLMWithReasoning()
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
                db_path=db,
                default_worktree=tmp,
                default_permission_action="ask",
                prompt_templates={},
            )
            loop = SessionLoop(
                cfg=cfg,
                bus=bus,
                store=store,
                perm=perm,
                tools=tools,
                llm=llm,
                interrupt=InterruptManager(),
                hooks=hooks,
            )

            await loop.run(session_id=sid)

            self.assertEqual(llm.calls, 2)
            second_call_messages = llm.inputs[1]
            assistant = next(m for m in second_call_messages if m.get("role") == "assistant")
            self.assertIn("tool_calls", assistant)
            self.assertEqual(assistant.get("reasoning_content"), "Need tool. ")

            history = await store.list_messages(sid)
            assistant_history = next(m for m in history if m.info.role == "assistant")
            reasoning_parts = [p for p in assistant_history.parts if getattr(p, "type", "") == "reasoning"]
            self.assertEqual(len(reasoning_parts), 1)
            self.assertEqual(reasoning_parts[0].text, "Need tool. ")
