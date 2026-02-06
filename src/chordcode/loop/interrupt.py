from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class InterruptSignal:
    session_id: str
    reason: str


class InterruptManager:
    """Manages interruption signals for session loops."""

    def __init__(self) -> None:
        self._signals: dict[str, InterruptSignal] = {}
        self._lock = asyncio.Lock()

    async def interrupt(self, session_id: str, reason: str = "user_cancelled") -> None:
        """Send an interrupt signal to a running session."""
        async with self._lock:
            self._signals[session_id] = InterruptSignal(session_id=session_id, reason=reason)

    async def check(self, session_id: str) -> Optional[InterruptSignal]:
        """Check if this session has been interrupted."""
        async with self._lock:
            return self._signals.get(session_id)

    async def clear(self, session_id: str) -> None:
        """Clear interrupt signal for a session."""
        async with self._lock:
            if session_id in self._signals:
                del self._signals[session_id]

    async def is_interrupted(self, session_id: str) -> bool:
        """Quick check if session is interrupted."""
        sig = await self.check(session_id)
        return sig is not None
