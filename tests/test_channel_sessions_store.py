from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.store.sqlite import SQLiteStore
from chordcode.model import PermissionRule, Session


class ChannelSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_bind_and_lookup_channel_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()

            self.assertIsNone(await store.get_channel_session("feishu", "oc_123"))

            await store.bind_channel_session(
                channel="feishu",
                chat_id="oc_123",
                session_id="s1",
                sender_id="ou_1",
            )
            self.assertEqual(await store.get_channel_session("feishu", "oc_123"), "s1")

            await store.bind_channel_session(
                channel="feishu",
                chat_id="oc_123",
                session_id="s2",
                sender_id="ou_2",
            )
            self.assertEqual(await store.get_channel_session("feishu", "oc_123"), "s2")

    async def test_update_session_permission_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()

            await store.create_session(
                Session(
                    id="s1",
                    title="t",
                    worktree=tmp,
                    cwd=tmp,
                    created_at=1,
                    updated_at=1,
                    permission_rules=[PermissionRule(permission="*", pattern="*", action="ask")],
                )
            )

            updated = await store.update_session_permission_rules(
                "s1",
                [PermissionRule(permission="*", pattern="*", action="deny")],
            )
            self.assertEqual(updated.permission_rules[0].action, "deny")
