from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Any, AsyncIterator


@dataclass(frozen=True)
class Event:
    type: str
    properties: dict[str, Any]


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subs = set(self._subs.get(event.type, set())) | set(self._subs.get("*", set()))
        for q in subs:
            q.put_nowait(event)

    async def subscribe(self, event_type: str = "*") -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        async with self._lock:
            s = self._subs.get(event_type, set())
            s.add(q)
            self._subs[event_type] = s

        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                s = self._subs.get(event_type, set())
                s.discard(q)
                if not s and event_type in self._subs:
                    del self._subs[event_type]

