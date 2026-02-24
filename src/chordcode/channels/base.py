from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from chordcode.channels.bus import ChannelBus
from chordcode.channels.events import InboundChannelMessage, OutboundChannelMessage
from chordcode.log import logger


class BaseChannel(ABC):
    """Base class for channel adapters."""

    name: str = "base"

    def __init__(self, config: Any, bus: ChannelBus) -> None:
        self.config = config
        self.bus = bus
        self._running = False
        self._log = logger.child(service=f"channel.{self.name}", channel=self.name)

    @abstractmethod
    async def start(self) -> None:
        """Start channel listener (usually long-running)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop channel listener and release resources."""

    @abstractmethod
    async def send(self, msg: OutboundChannelMessage) -> None:
        """Send outbound message to channel."""

    async def test_connection(self) -> dict[str, Any]:
        """Run adapter-specific connection/credential test."""
        return {"ok": False, "channel": self.name, "message": "test_connection is not implemented"}

    def is_allowed(self, sender_id: str) -> bool:
        allow_list = getattr(self.config, "allow_from", []) or []
        if not allow_list:
            return True
        sender_str = str(sender_id)
        if sender_str in allow_list:
            return True
        if "|" in sender_str:
            return any(part and part in allow_list for part in sender_str.split("|"))
        return False

    async def _handle_message(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_allowed(sender_id):
            self._log.warning(
                "Inbound message rejected by allow list",
                event="channel.inbound.rejected",
                sender_id=sender_id,
                chat_id=chat_id,
            )
            return

        msg = InboundChannelMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
        )
        await self.bus.publish_inbound(msg)
        self._log.debug(
            "Inbound message published",
            event="channel.inbound.published",
            sender_id=msg.sender_id,
            chat_id=msg.chat_id,
            content_chars=len(msg.content or ""),
            media_count=len(msg.media),
        )

    @property
    def is_running(self) -> bool:
        return self._running
