from __future__ import annotations

import asyncio

from chordcode.channels.events import InboundChannelMessage, OutboundChannelMessage


class ChannelBus:
    """Async queue bus that decouples channel adapters from agent processing."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundChannelMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundChannelMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundChannelMessage) -> None:
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundChannelMessage:
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundChannelMessage) -> None:
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundChannelMessage:
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self.outbound.qsize()

