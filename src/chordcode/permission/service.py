from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from chordcode.bus.bus import Bus, Event
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker
from chordcode.log import logger
from chordcode.model import PermissionReply, PermissionRequest, PermissionRule
from chordcode.permission.rules import evaluate_permission
from chordcode.store.sqlite import SQLiteStore


class PermissionRejected(Exception):
    pass


class PermissionService:
    def __init__(self, bus: Bus, store: SQLiteStore, hooks: Hooker | None = None) -> None:
        self._bus = bus
        self._store = store
        self._hooks = hooks
        self._pending: dict[str, asyncio.Future[PermissionReply]] = {}
        self._lock = asyncio.Lock()

    async def ask(
        self,
        *,
        session_id: str,
        ruleset: list[PermissionRule],
        permission: str,
        patterns: list[str],
        metadata: dict[str, Any],
        always: list[str],
        tool: dict[str, str] | None = None,
    ) -> None:
        approvals = await self._store.list_approvals(session_id)
        approved_rules = [PermissionRule.model_validate(r) for r in approvals]

        for p in patterns:
            d = evaluate_permission(permission, p, ruleset + approved_rules)
            if d.action == "allow":
                continue
            if d.action == "deny":
                raise PermissionRejected(f"permission denied: {permission} {p}")

            if self._hooks:
                out: dict[str, object] = {"status": "ask"}
                await self._hooks.trigger(
                    Hook.PermissionAsk,
                    {
                        "session_id": session_id,
                        "permission": permission,
                        "pattern": p,
                        "patterns": patterns,
                        "metadata": metadata,
                        "always": always,
                        "tool": tool,
                    },
                    out,
                )
                status = str(out.get("status") or "ask")
                if status == "allow":
                    continue
                if status == "deny":
                    raise PermissionRejected(f"permission denied: {permission} {p}")

            source = str(metadata.get("source", "") or "").strip()
            if source.startswith("channel:"):
                req = PermissionRequest(
                    id=str(uuid4()),
                    session_id=session_id,
                    permission=permission,
                    patterns=patterns,
                    metadata=metadata,
                    always=always,
                    tool=tool,
                )
                await self._store.create_permission_request(req)
                await self._bus.publish(Event(type="permission.asked", properties=req.model_dump()))
                await self._store.resolve_permission_request(req.id, "rejected")
                await self._bus.publish(
                    Event(
                        type="permission.replied",
                        properties={
                            "session_id": session_id,
                            "request_id": req.id,
                            "reply": "reject",
                            "reason": "channel_auto_reject",
                        },
                    ),
                )
                logger.warning(
                    "Permission ask auto-rejected for channel source",
                    event="permission.channel.auto_reject",
                    session_id=session_id,
                    source=source,
                    permission=permission,
                    pattern=p,
                )
                raise PermissionRejected(
                    f"permission requires interactive approval: {permission} {p}; channel source does not support approval flow"
                )

            req = PermissionRequest(
                id=str(uuid4()),
                session_id=session_id,
                permission=permission,
                patterns=patterns,
                metadata=metadata,
                always=always,
                tool=tool,
            )
            await self._store.create_permission_request(req)
            await self._bus.publish(Event(type="permission.asked", properties=req.model_dump()))

            fut: asyncio.Future[PermissionReply] = asyncio.get_event_loop().create_future()
            async with self._lock:
                self._pending[req.id] = fut
            try:
                reply = await fut
            finally:
                async with self._lock:
                    current = self._pending.get(req.id)
                    if current is fut:
                        del self._pending[req.id]

            if reply.reply == "reject":
                await self._store.resolve_permission_request(req.id, "rejected")
                raise PermissionRejected(reply.message or "rejected")

            if reply.reply == "once":
                await self._store.resolve_permission_request(req.id, "once")
                await self._bus.publish(
                    Event(
                        type="permission.replied",
                        properties={"session_id": session_id, "request_id": req.id, "reply": "once"},
                    ),
                )
                return

            await self._store.resolve_permission_request(req.id, "always")
            for a in always:
                await self._store.add_approval(session_id, permission, a, "allow")
            await self._bus.publish(
                Event(
                    type="permission.replied",
                    properties={"session_id": session_id, "request_id": req.id, "reply": "always"},
                ),
            )
            return

    async def reply(self, request_id: str, reply: PermissionReply) -> None:
        async with self._lock:
            fut = self._pending.get(request_id)
            if not fut:
                return
            if fut.done():
                return
            fut.set_result(reply)
