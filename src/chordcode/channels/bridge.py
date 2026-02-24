from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from chordcode.channels.bus import ChannelBus
from chordcode.channels.events import InboundChannelMessage, OutboundChannelMessage
from chordcode.log import logger
from chordcode.observability.langfuse_client import get_langfuse

ProcessInbound = Callable[[InboundChannelMessage], Awaitable[OutboundChannelMessage | None]]


class ChannelSessionBridge:
    """Consume inbound channel messages, call agent workflow, publish outbound responses."""

    def __init__(self, *, bus: ChannelBus, process_inbound: ProcessInbound) -> None:
        self._bus = bus
        self._process_inbound = process_inbound
        self._task: asyncio.Task[None] | None = None
        self._log = logger.child(service="channel.bridge")

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        self._log.info("Channel bridge started", event="channel.bridge.start")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._log.info("Channel bridge stopped", event="channel.bridge.stop")

    @property
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _run(self) -> None:
        while True:
            try:
                msg = await self._bus.consume_inbound()
                await self._handle_one(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error("Bridge loop error", event="channel.bridge.loop.error", exc_info=e)

    async def _handle_one(self, msg: InboundChannelMessage) -> None:
        self._log.info(
            "Inbound message received by bridge",
            event="channel.bridge.inbound",
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            content_chars=len(msg.content or ""),
            media_count=len(msg.media),
        )

        langfuse = get_langfuse()
        outbound: OutboundChannelMessage | None = None
        if langfuse:
            try:
                with langfuse.start_as_current_observation(
                    name="channel-inbound",
                    as_type="span",
                    input={"content": msg.content},
                    metadata={
                        "channel": msg.channel,
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "media_count": len(msg.media),
                    },
                ) as span:
                    outbound = await self._process_inbound(msg)
                    if span:
                        span.update(
                            output={"has_response": bool(outbound and outbound.content.strip())},
                            metadata={"response_channel": outbound.channel if outbound else None},
                        )
            except Exception as e:
                self._log.error("Channel bridge Langfuse error", event="channel.bridge.langfuse.error", exc_info=e)
                outbound = await self._process_inbound(msg)
        else:
            outbound = await self._process_inbound(msg)

        if outbound:
            await self._bus.publish_outbound(outbound)
            self._log.info(
                "Outbound message published by bridge",
                event="channel.bridge.outbound",
                channel=outbound.channel,
                chat_id=outbound.chat_id,
                content_chars=len(outbound.content or ""),
                media_count=len(outbound.media),
            )

