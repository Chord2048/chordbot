from __future__ import annotations

import asyncio
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.channels.bus import ChannelBus
from chordcode.channels.feishu import FeishuChannel
from chordcode.config import FeishuChannelConfig


class _FakeFeishuClientBuilder:
    def app_id(self, _value: str):
        return self

    def app_secret(self, _value: str):
        return self

    def log_level(self, _value: object):
        return self

    def build(self):
        return object()


class _FakeEventHandlerBuilder:
    def register_p2_im_message_receive_v1(self, _handler):
        return self

    def build(self):
        return object()


class FeishuChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_client_uses_dedicated_thread_loop(self) -> None:
        start_called = threading.Event()
        stop_event = threading.Event()
        observed: dict[str, object] = {}

        class _FakeWSClient:
            def __init__(self, *_args, **_kwargs):
                self._auto_reconnect = True

            def start(self) -> None:
                import lark_oapi.ws.client as ws_client_module

                loop = ws_client_module.loop
                observed["loop"] = ws_client_module.loop
                observed["thread_id"] = threading.get_ident()
                observed["loop_running"] = ws_client_module.loop.is_running()
                start_called.set()

                async def _wait_for_stop() -> None:
                    while not stop_event.is_set():
                        await asyncio.sleep(0.01)
                    raise RuntimeError("stop requested")

                loop.run_until_complete(_wait_for_stop())

            async def _disconnect(self) -> None:
                stop_event.set()

        fake_lark = types.SimpleNamespace(
            Client=types.SimpleNamespace(builder=lambda: _FakeFeishuClientBuilder()),
            EventDispatcherHandler=types.SimpleNamespace(builder=lambda _enc, _token: _FakeEventHandlerBuilder()),
            ws=types.SimpleNamespace(Client=_FakeWSClient),
            LogLevel=types.SimpleNamespace(INFO=20),
        )

        cfg = FeishuChannelConfig(
            enabled=True,
            app_id="cli_test_app_id",
            app_secret="cli_test_secret",
            encrypt_key="",
            verification_token="",
            allow_from=[],
        )
        channel = FeishuChannel(config=cfg, bus=ChannelBus())
        start_task: asyncio.Task[None] | None = None

        import chordcode.channels.feishu as feishu_module

        with (
            mock.patch.object(feishu_module, "FEISHU_AVAILABLE", True),
            mock.patch.object(feishu_module, "lark", fake_lark),
        ):
            try:
                start_task = asyncio.create_task(channel.start())
                started = await asyncio.to_thread(start_called.wait, 1.5)
                self.assertTrue(started, "feishu websocket start was not called")

                main_loop = asyncio.get_running_loop()
                self.assertNotEqual(observed.get("thread_id"), threading.get_ident())
                self.assertIsNot(observed.get("loop"), main_loop)
                observed_loop = observed.get("loop")
                self.assertIsNotNone(observed_loop)
                self.assertFalse(bool(getattr(observed_loop, "is_closed", lambda: True)()))
            finally:
                await channel.stop()
                if start_task is not None:
                    await asyncio.wait_for(start_task, 2.0)

        self.assertTrue(channel._ws_thread is None or not channel._ws_thread.is_alive())
