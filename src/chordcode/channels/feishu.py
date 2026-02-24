from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from collections import OrderedDict
from typing import Any

import httpx

from chordcode.channels.base import BaseChannel
from chordcode.channels.bus import ChannelBus
from chordcode.channels.events import OutboundChannelMessage

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        P2ImMessageReceiveV1,
    )

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    P2ImMessageReceiveV1 = Any


def _extract_post_text(content_json: dict[str, Any]) -> str:
    """Extract plain text from Feishu post (rich text) message content."""

    def extract_from_lang(lang_content: dict[str, Any]) -> str | None:
        if not isinstance(lang_content, dict):
            return None
        title = str(lang_content.get("title", "") or "")
        content_blocks = lang_content.get("content", [])
        if not isinstance(content_blocks, list):
            return None
        text_parts: list[str] = []
        if title:
            text_parts.append(title)
        for block in content_blocks:
            if not isinstance(block, list):
                continue
            for element in block:
                if not isinstance(element, dict):
                    continue
                tag = element.get("tag")
                if tag in {"text", "a"}:
                    text_parts.append(str(element.get("text", "") or ""))
                elif tag == "at":
                    user_name = str(element.get("user_name", "user") or "user")
                    text_parts.append(f"@{user_name}")
        result = " ".join([t for t in text_parts if t.strip()]).strip()
        return result or None

    if "content" in content_json:
        direct = extract_from_lang(content_json)
        if direct:
            return direct

    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        localized = extract_from_lang(content_json.get(lang_key, {}))
        if localized:
            return localized
    return ""


class FeishuChannel(BaseChannel):
    """Feishu adapter via lark-oapi WebSocket long connection."""

    name = "feishu"

    def __init__(self, config: Any, bus: ChannelBus) -> None:
        super().__init__(config, bus)
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_runtime_loop: asyncio.AbstractEventLoop | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()

    async def start(self) -> None:
        if not FEISHU_AVAILABLE:
            self._log.error(
                "Feishu SDK missing; install with `pip install lark-oapi`",
                event="channel.feishu.sdk_missing",
            )
            return
        if not self.config.app_id or not self.config.app_secret:
            self._log.error(
                "Feishu channel is enabled but app_id/app_secret is missing",
                event="channel.feishu.config_missing",
            )
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.config.encrypt_key or "",
                self.config.verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        def run_ws() -> None:
            import lark_oapi.ws.client as ws_client_module
            import time

            while self._running:
                loop = asyncio.new_event_loop()
                self._ws_runtime_loop = loop
                try:
                    asyncio.set_event_loop(loop)
                    # lark-oapi websocket client uses a module-level loop object.
                    # Re-bind it per thread so start() does not operate on app main loop.
                    ws_client_module.loop = loop
                    self._ws_client.start()
                except Exception as e:
                    if self._running:
                        self._log.warning(
                            "Feishu websocket loop error",
                            event="channel.feishu.ws.error",
                            exc_info=e,
                        )
                    else:
                        self._log.info(
                            "Feishu websocket loop stopped",
                            event="channel.feishu.ws.stopped",
                            reason=type(e).__name__,
                        )
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    except Exception:
                        pass
                    try:
                        loop.close()
                    except Exception:
                        pass
                    self._ws_runtime_loop = None
                if self._running:
                    time.sleep(5)

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        self._log.info(
            "Feishu channel started",
            event="channel.feishu.start",
            allow_from_count=len(self.config.allow_from),
        )

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        if self._ws_client:
            try:
                setattr(self._ws_client, "_auto_reconnect", False)
            except Exception as e:
                self._log.warning("Error disabling Feishu auto reconnect", event="channel.feishu.stop.autoreconnect_error", exc_info=e)

        ws_loop = self._ws_runtime_loop
        if ws_loop and ws_loop.is_running() and self._ws_client:
            disconnect = getattr(self._ws_client, "_disconnect", None)
            if callable(disconnect):
                try:
                    future = asyncio.run_coroutine_threadsafe(disconnect(), ws_loop)
                    future.result(timeout=3)
                except FutureTimeoutError:
                    self._log.warning("Feishu websocket disconnect timeout", event="channel.feishu.stop.disconnect_timeout")
                except Exception as e:
                    self._log.warning("Error disconnecting Feishu websocket", event="channel.feishu.stop.disconnect_error", exc_info=e)

            try:
                ws_loop.call_soon_threadsafe(ws_loop.stop)
            except Exception as e:
                self._log.warning("Error stopping Feishu websocket loop", event="channel.feishu.stop.loop_error", exc_info=e)

        if self._ws_thread and self._ws_thread.is_alive():
            await asyncio.to_thread(self._ws_thread.join, 5)
            if self._ws_thread.is_alive():
                self._log.warning("Feishu websocket thread still alive after stop timeout", event="channel.feishu.stop.thread_timeout")

        self._log.info("Feishu channel stopped", event="channel.feishu.stop")

    def _send_text_sync(self, receive_id_type: str, receive_id: str, content: str) -> bool:
        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("text")
                    .content(json.dumps({"text": content}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                self._log.error(
                    "Failed to send Feishu message",
                    event="channel.feishu.send.error",
                    receive_id=receive_id,
                    response_code=response.code,
                    response_message=response.msg,
                )
                return False
            return True
        except Exception as e:
            self._log.error("Unexpected Feishu send error", event="channel.feishu.send.exception", exc_info=e)
            return False

    async def send(self, msg: OutboundChannelMessage) -> None:
        if not self._client:
            self._log.warning("Feishu client is not initialized", event="channel.feishu.send.not_ready")
            return
        if not msg.content.strip():
            self._log.debug("Skip empty Feishu outbound message", event="channel.feishu.send.skip_empty")
            return

        receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self._send_text_sync, receive_id_type, msg.chat_id, msg.content)
        if ok:
            self._log.info(
                "Feishu outbound delivered",
                event="channel.feishu.send.ok",
                chat_id=msg.chat_id,
                content_chars=len(msg.content),
            )

    def _on_message_sync(self, data: P2ImMessageReceiveV1) -> None:
        if self._loop and self._loop.is_running():
            coro = self._on_message(data)
            try:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
            except Exception as e:
                coro.close()
                self._log.warning("Failed to hand off Feishu inbound message to app loop", event="channel.feishu.inbound.handoff_error", exc_info=e)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            event = data.event
            if not event:
                return
            message = event.message
            sender = event.sender
            if not message or not sender:
                return

            message_id = str(message.message_id or "")
            if not message_id:
                return
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            if getattr(sender, "sender_type", "") == "bot":
                return

            sender_id = str(getattr(getattr(sender, "sender_id", None), "open_id", "") or "unknown")
            chat_id = str(message.chat_id or "")
            chat_type = str(message.chat_type or "")
            msg_type = str(message.message_type or "")

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            content = ""
            if msg_type == "text":
                content = str(content_json.get("text", "") or "")
            elif msg_type == "post":
                content = _extract_post_text(content_json)
            else:
                # Non-text input types are intentionally summarized in this phase.
                content = f"[{msg_type}]"

            if not content.strip():
                return

            reply_target = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_target,
                content=content,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                },
            )
            self._log.info(
                "Feishu inbound processed",
                event="channel.feishu.inbound",
                sender_id=sender_id,
                chat_id=reply_target,
                chat_type=chat_type,
                msg_type=msg_type,
                content_chars=len(content),
            )
        except Exception as e:
            self._log.error("Error processing Feishu inbound", event="channel.feishu.inbound.error", exc_info=e)

    async def test_connection(self) -> dict[str, Any]:
        if not FEISHU_AVAILABLE:
            return {
                "ok": False,
                "channel": self.name,
                "message": "lark-oapi not installed",
                "error_type": "sdk_missing",
            }
        if not self.config.app_id or not self.config.app_secret:
            return {
                "ok": False,
                "channel": self.name,
                "message": "missing app_id/app_secret",
                "error_type": "config_missing",
            }

        url = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
        payload = {"app_id": self.config.app_id, "app_secret": self.config.app_secret}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
            data = response.json() if response.content else {}
            code = int(data.get("code", -1))
            if response.status_code != 200 or code != 0:
                msg = str(data.get("msg", "") or f"http_{response.status_code}")
                return {
                    "ok": False,
                    "channel": self.name,
                    "message": f"auth failed: {msg}",
                    "status_code": response.status_code,
                    "error_code": code,
                }
            token = str(data.get("app_access_token", "") or "")
            return {
                "ok": True,
                "channel": self.name,
                "message": "auth success",
                "token_prefix": token[:8] if token else "",
                "expires_in": int(data.get("expire", 0) or 0),
                "running": self.is_running,
            }
        except Exception as e:
            return {
                "ok": False,
                "channel": self.name,
                "message": f"connection test failed: {e}",
                "error_type": type(e).__name__,
            }
