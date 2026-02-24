from __future__ import annotations

import asyncio
from typing import Any

from chordcode.channels.base import BaseChannel
from chordcode.channels.bus import ChannelBus
from chordcode.channels.events import OutboundChannelMessage
from chordcode.config import Config
from chordcode.log import logger


class ChannelManager:
    """Channel lifecycle manager + outbound dispatcher."""

    def __init__(self, config: Config, bus: ChannelBus) -> None:
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._channel_tasks: dict[str, asyncio.Task[None]] = {}
        self._log = logger.child(service="channel.manager")
        self._init_channels()

    def _init_channels(self) -> None:
        if self.config.channels.feishu.enabled:
            try:
                from chordcode.channels.feishu import FeishuChannel

                self.channels["feishu"] = FeishuChannel(self.config.channels.feishu, self.bus)
                self._log.info("Channel enabled", event="channel.enabled", channel="feishu")
            except Exception as e:
                self._log.error("Failed to initialize Feishu channel", event="channel.init.error", channel="feishu", exc_info=e)

    async def start_all(self) -> None:
        if not self.channels:
            self._log.info("No channels enabled", event="channel.none_enabled")
            return
        if not self._dispatch_task:
            self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        for name in self.channels.keys():
            await self.connect_channel(name)
        self._log.info(
            "Channel manager started",
            event="channel.manager.start",
            channels=",".join(self.enabled_channels),
        )

    async def stop_all(self) -> None:
        for name, task in list(self._channel_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._log.error("Channel task stop failed", event="channel.stop.error", channel=name, exc_info=e)
        self._channel_tasks.clear()

        for name, channel in self.channels.items():
            try:
                await channel.stop()
            except Exception as e:
                self._log.error("Channel stop failed", event="channel.stop.error", channel=name, exc_info=e)

        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._log.error("Outbound dispatcher stop failed", event="channel.dispatcher.stop.error", exc_info=e)
            self._dispatch_task = None

        self._log.info("Channel manager stopped", event="channel.manager.stop")

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        try:
            await channel.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.error("Channel start failed", event="channel.start.error", channel=name, exc_info=e)
        finally:
            # Remove task handle when this coroutine exits so reconnect can create a new one.
            current = asyncio.current_task()
            task = self._channel_tasks.get(name)
            if task is current:
                self._channel_tasks.pop(name, None)

    async def _dispatch_outbound(self) -> None:
        self._log.info("Outbound dispatcher started", event="channel.dispatcher.start")
        while True:
            try:
                msg: OutboundChannelMessage = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)
                channel = self.channels.get(msg.channel)
                if not channel:
                    self._log.warning(
                        "Outbound dropped: unknown channel",
                        event="channel.dispatch.unknown_channel",
                        channel=msg.channel,
                    )
                    continue
                await channel.send(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error("Outbound dispatch error", event="channel.dispatch.error", exc_info=e)

    def get_channel(self, name: str) -> BaseChannel | None:
        return self.channels.get(name)

    async def connect_channel(self, name: str) -> None:
        channel = self.channels.get(name)
        if not channel:
            raise KeyError(f"channel not found: {name}")
        existing = self._channel_tasks.get(name)
        if existing and not existing.done():
            return
        self._channel_tasks[name] = asyncio.create_task(self._start_channel(name, channel))
        self._log.info("Channel connect requested", event="channel.connect.request", channel=name)

    async def disconnect_channel(self, name: str) -> None:
        channel = self.channels.get(name)
        if not channel:
            raise KeyError(f"channel not found: {name}")

        task = self._channel_tasks.pop(name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._log.error("Channel task cancel error", event="channel.disconnect.task.error", channel=name, exc_info=e)

        await channel.stop()
        self._log.info("Channel disconnected", event="channel.disconnect", channel=name)

    async def test_channel(self, name: str) -> dict[str, Any]:
        channel = self.channels.get(name)
        if not channel:
            raise KeyError(f"channel not found: {name}")
        return await channel.test_connection()

    def get_status(self) -> dict[str, Any]:
        return {
            name: {"enabled": True, "running": channel.is_running}
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        return list(self.channels.keys())
