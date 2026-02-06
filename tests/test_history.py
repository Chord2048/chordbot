from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.model import (
    Message,
    ModelRef,
    ReasoningPart,
    Session,
    TextPart,
    ToolPart,
    ToolStateCompleted,
)
from chordcode.store.sqlite import SQLiteStore


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_messages_returns_parts_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db)
            await store.init()

            sid = "s1"
            await store.create_session(
                Session(
                    id=sid,
                    title="t",
                    worktree=tmp,
                    cwd=tmp,
                    created_at=1,
                    updated_at=1,
                    permission_rules=[],
                )
            )

            user = Message(
                id="u1",
                session_id=sid,
                role="user",
                parent_id=None,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="m"),
                created_at=1,
            )
            await store.add_message(user)
            await store.add_part(
                sid,
                user.id,
                TextPart(id="p1", message_id=user.id, session_id=sid, text="hello"),
            )

            assistant = Message(
                id="a1",
                session_id=sid,
                role="assistant",
                parent_id=user.id,
                agent="primary",
                model=ModelRef(provider="openai-compatible", id="m"),
                created_at=2,
                error={"message": "boom"},
            )
            await store.add_message(assistant)
            await store.add_part(
                sid,
                assistant.id,
                ReasoningPart(id="r1", message_id=assistant.id, session_id=sid, text="thinking", time={"start": 0, "end": 1}),
            )
            await store.add_part(
                sid,
                assistant.id,
                ToolPart(
                    id="t1",
                    message_id=assistant.id,
                    session_id=sid,
                    call_id="call1",
                    tool="echo",
                    state=ToolStateCompleted(
                        input={"x": 1},
                        title="echo",
                        output="1",
                        metadata={},
                        time={"start": 0, "end": 1},
                    ),
                ),
            )
            await store.add_part(
                sid,
                assistant.id,
                TextPart(id="p2", message_id=assistant.id, session_id=sid, text="done"),
            )

            history = await store.list_messages(sid)
            self.assertEqual([m.info.id for m in history], ["u1", "a1"])

            user_parts = history[0].parts
            self.assertEqual([p.type for p in user_parts], ["text"])
            self.assertEqual(getattr(user_parts[0], "text", ""), "hello")

            assistant_parts = history[1].parts
            self.assertEqual([p.type for p in assistant_parts], ["reasoning", "tool", "text"])
            self.assertEqual(history[1].info.error, {"message": "boom"})
