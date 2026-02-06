from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.bus.bus import Bus, Event
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker
from chordcode.model import PermissionReply, PermissionRule, Session
from chordcode.permission.service import PermissionRejected, PermissionService
from chordcode.store.sqlite import SQLiteStore


class PermissionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "db.sqlite3")
        self.store = SQLiteStore(self.db_path)
        await self.store.init()

        self.session_id = "s1"
        await self.store.create_session(
            Session(
                id=self.session_id,
                title="t",
                worktree=self._tmp.name,
                cwd=self._tmp.name,
                created_at=1,
                updated_at=1,
                permission_rules=[],
            )
        )

        self.bus = Bus()
        self.hooks = Hooker()
        self.perm = PermissionService(self.bus, self.store, self.hooks)

    async def test_allows_when_ruleset_allows(self) -> None:
        await self.perm.ask(
            session_id=self.session_id,
            ruleset=[PermissionRule(permission="read", pattern="*", action="allow")],
            permission="read",
            patterns=["/tmp/x"],
            metadata={},
            always=["*"],
        )
        pending = await self.store.list_pending_permission_requests(self.session_id)
        self.assertEqual(pending, [])

    async def test_denies_when_ruleset_denies(self) -> None:
        with self.assertRaises(PermissionRejected):
            await self.perm.ask(
                session_id=self.session_id,
                ruleset=[PermissionRule(permission="read", pattern="*", action="deny")],
                permission="read",
                patterns=["/tmp/x"],
                metadata={},
                always=["*"],
            )

    async def test_ask_once_resolves_and_emits_events(self) -> None:
        asked_iter = self.bus.subscribe("permission.asked")
        replied_iter = self.bus.subscribe("permission.replied")

        async def do_ask():
            await self.perm.ask(
                session_id=self.session_id,
                ruleset=[PermissionRule(permission="read", pattern="*", action="ask")],
                permission="read",
                patterns=["/tmp/x"],
                metadata={"k": "v"},
                always=["*"],
            )

        ask_task = asyncio.create_task(do_ask())
        asked = await asyncio.wait_for(asked_iter.__anext__(), timeout=2)
        request_id = str(asked.properties["id"])

        await self.perm.reply(request_id, PermissionReply(reply="once"))

        replied = await asyncio.wait_for(replied_iter.__anext__(), timeout=2)
        self.assertEqual(replied.type, "permission.replied")
        self.assertEqual(replied.properties.get("request_id"), request_id)
        self.assertEqual(replied.properties.get("reply"), "once")

        await asyncio.wait_for(ask_task, timeout=2)
        await asked_iter.aclose()
        await replied_iter.aclose()

        pending = await self.store.list_pending_permission_requests(self.session_id)
        self.assertEqual(pending, [])

    async def test_ask_always_persists_approval_and_skips_future_asks(self) -> None:
        asked_iter = self.bus.subscribe("permission.asked")

        async def do_ask():
            await self.perm.ask(
                session_id=self.session_id,
                ruleset=[PermissionRule(permission="bash", pattern="*", action="ask")],
                permission="bash",
                patterns=["git status"],
                metadata={},
                always=["git*"],
            )

        ask_task = asyncio.create_task(do_ask())
        asked = await asyncio.wait_for(asked_iter.__anext__(), timeout=2)
        request_id = str(asked.properties["id"])

        await self.perm.reply(request_id, PermissionReply(reply="always"))
        await asyncio.wait_for(ask_task, timeout=2)
        await asked_iter.aclose()

        approvals = await self.store.list_approvals(self.session_id)
        self.assertIn({"permission": "bash", "pattern": "git*", "action": "allow"}, approvals)

        # Subsequent requests should be auto-allowed by persisted approvals.
        await self.perm.ask(
            session_id=self.session_id,
            ruleset=[],
            permission="bash",
            patterns=["git diff"],
            metadata={},
            always=["git*"],
        )
        pending = await self.store.list_pending_permission_requests(self.session_id)
        self.assertEqual(pending, [])

    async def test_permission_ask_hook_can_short_circuit(self) -> None:
        async def allow(_input, output):
            output["status"] = "allow"

        self.hooks.add({Hook.PermissionAsk: allow})

        await self.perm.ask(
            session_id=self.session_id,
            ruleset=[PermissionRule(permission="read", pattern="*", action="ask")],
            permission="read",
            patterns=["/tmp/x"],
            metadata={},
            always=["*"],
        )
        pending = await self.store.list_pending_permission_requests(self.session_id)
        self.assertEqual(pending, [])
