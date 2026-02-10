from __future__ import annotations

import asyncio
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from chordcode.bus.bus import Bus, Event
from chordcode.log import logger
from chordcode.mcp.config import MCPServerConfig

_log = logger.child(service="mcp")

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_\-]")


def _sanitize(name: str) -> str:
    return _SANITIZE_RE.sub("_", name)


def _namespace(server: str, tool: str) -> str:
    return f"{_sanitize(server)}_{_sanitize(tool)}"


def _normalize_schema(raw: dict[str, Any]) -> dict[str, Any]:
    """Force type=object, default empty properties, additionalProperties=false."""
    schema = dict(raw) if raw else {}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("additionalProperties", False)
    return schema


@dataclass(frozen=True)
class MCPServerStatus:
    name: str
    status: Literal["connected", "disabled", "failed", "connecting"]
    error: str = ""
    tool_count: int = 0


@dataclass(frozen=True)
class MCPToolInfo:
    server_name: str
    tool_name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class MCPCallResult:
    content: str
    is_error: bool
    raw_content: list[Any]


class MCPManager:
    """Process-level singleton managing MCP server connections."""

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._configs: dict[str, MCPServerConfig] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._statuses: dict[str, MCPServerStatus] = {}
        self._tools_cache: dict[str, list[MCPToolInfo]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self, configs: dict[str, MCPServerConfig]) -> None:
        self._configs = dict(configs)
        enabled = {n: c for n, c in configs.items() if c.enabled}
        disabled = {n: c for n, c in configs.items() if not c.enabled}

        for name in disabled:
            self._statuses[name] = MCPServerStatus(
                name=name, status="disabled",
            )

        if not enabled:
            return

        results = await asyncio.gather(
            *(self._connect(name, cfg) for name, cfg in enabled.items()),
            return_exceptions=True,
        )
        for (name, _), result in zip(enabled.items(), results):
            if isinstance(result, BaseException):
                err = f"{type(result).__name__}: {result}"
                self._statuses[name] = MCPServerStatus(
                    name=name, status="failed", error=err,
                )
                _log.error(
                    f"mcp server '{name}' connect failed: {err}",
                    event="mcp.server.failed",
                )
                await self._bus.publish(Event(
                    type="mcp.server.failed",
                    properties={"server": name, "error": err},
                ))

    async def _connect(self, name: str, config: MCPServerConfig) -> None:
        """Connect to a single MCP server and cache its tools."""
        _log.debug(f"mcp connecting to '{name}'", event="mcp.server.connecting")
        self._statuses[name] = MCPServerStatus(name=name, status="connecting")

        stack = AsyncExitStack()
        try:
            if config.type == "local":
                session = await self._connect_local(name, config, stack)
            else:
                session, stack = await self._connect_remote(name, config, stack)

            # Cache tools
            result = await session.list_tools()
            tools: list[MCPToolInfo] = []
            for t in result.tools:
                tools.append(MCPToolInfo(
                    server_name=name,
                    tool_name=t.name,
                    namespaced_name=_namespace(name, t.name),
                    description=t.description or "",
                    input_schema=_normalize_schema(t.inputSchema),
                ))

            async with self._lock:
                self._sessions[name] = session
                self._stacks[name] = stack
                self._tools_cache[name] = tools
                self._statuses[name] = MCPServerStatus(
                    name=name, status="connected", tool_count=len(tools),
                )

            _log.info(
                f"mcp server '{name}' connected, {len(tools)} tool(s)",
                event="mcp.server.connected",
            )
            await self._bus.publish(Event(
                type="mcp.server.connected",
                properties={"server": name, "tool_count": len(tools)},
            ))
        except Exception:
            await stack.aclose()
            raise

    async def _connect_local(
        self, name: str, config: MCPServerConfig, stack: AsyncExitStack,
    ) -> ClientSession:
        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=config.env or None,
        )
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(params),
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream),
        )
        await session.initialize()
        return session

    async def _connect_remote(
        self, name: str, config: MCPServerConfig, stack: AsyncExitStack,
    ) -> tuple[ClientSession, AsyncExitStack]:
        if config.transport == "sse":
            return await self._connect_sse(name, config, stack), stack

        # Default: try streamable-http first, fall back to SSE
        try:
            session = await self._connect_streamable_http(name, config, stack)
            return session, stack
        except Exception as exc:
            _log.debug(
                f"mcp server '{name}' streamable-http failed ({exc}), trying SSE",
                event="mcp.server.connecting",
            )
            # Close the failed stack and create a fresh one
            await stack.aclose()
            new_stack = AsyncExitStack()
            session = await self._connect_sse(name, config, new_stack)
            return session, new_stack

    async def _connect_streamable_http(
        self, name: str, config: MCPServerConfig, stack: AsyncExitStack,
    ) -> ClientSession:
        import httpx
        http_client: httpx.AsyncClient | None = None
        if config.headers:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=config.headers),
            )
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamable_http_client(url=config.url, http_client=http_client),
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream),
        )
        await session.initialize()
        return session

    async def _connect_sse(
        self, name: str, config: MCPServerConfig, stack: AsyncExitStack,
    ) -> ClientSession:
        read_stream, write_stream = await stack.enter_async_context(
            sse_client(
                url=config.url,
                headers=config.headers or None,
                timeout=float(config.timeout),
            ),
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream),
        )
        await session.initialize()
        return session

    # -- public API --

    async def list_tools(self) -> list[MCPToolInfo]:
        async with self._lock:
            tools: list[MCPToolInfo] = []
            for server_tools in self._tools_cache.values():
                tools.extend(server_tools)
            return tools

    async def call_tool(
        self, server_name: str, tool_name: str, args: dict[str, Any],
    ) -> MCPCallResult:
        async with self._lock:
            session = self._sessions.get(server_name)
        if session is None:
            return MCPCallResult(
                content=f"MCP server '{server_name}' is not connected",
                is_error=True,
                raw_content=[],
            )

        _log.debug(
            f"mcp call {server_name}/{tool_name}",
            event="mcp.tool.start",
            tool_name=f"{server_name}/{tool_name}",
        )
        try:
            result = await session.call_tool(
                name=tool_name,
                arguments=args,
                read_timeout_seconds=timedelta(
                    seconds=self._configs.get(server_name, MCPServerConfig(name="", type="local")).timeout,
                ),
            )
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            _log.warning(
                f"mcp call {server_name}/{tool_name} error: {err}",
                event="mcp.tool.error",
            )
            return MCPCallResult(content=err, is_error=True, raw_content=[])

        # Extract text from content blocks
        texts: list[str] = []
        raw: list[Any] = []
        for block in result.content:
            raw.append(block)
            if hasattr(block, "text"):
                texts.append(block.text)
            else:
                texts.append(str(block))

        content = "\n".join(texts)
        _log.debug(
            f"mcp call {server_name}/{tool_name} done, is_error={result.isError}",
            event="mcp.tool.finish",
        )
        return MCPCallResult(
            content=content, is_error=result.isError, raw_content=raw,
        )

    async def connect(self, name: str) -> None:
        """(Re)connect a single server by name."""
        config = self._configs.get(name)
        if config is None:
            raise KeyError(f"unknown MCP server: {name}")
        # Disconnect first if already connected
        await self.disconnect(name)
        try:
            await self._connect(name, config)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._statuses[name] = MCPServerStatus(
                name=name, status="failed", error=err,
            )
            _log.error(
                f"mcp server '{name}' reconnect failed: {err}",
                event="mcp.server.failed",
            )
            raise

    async def disconnect(self, name: str) -> None:
        """Disconnect a single server."""
        async with self._lock:
            stack = self._stacks.pop(name, None)
            self._sessions.pop(name, None)
            self._tools_cache.pop(name, None)
        if stack:
            try:
                await stack.aclose()
            except Exception:
                pass
            self._statuses[name] = MCPServerStatus(name=name, status="disabled")
            _log.info(f"mcp server '{name}' disconnected", event="mcp.server.disconnected")
            await self._bus.publish(Event(
                type="mcp.server.disconnected",
                properties={"server": name},
            ))

    async def add_server(self, name: str, config: MCPServerConfig) -> None:
        """Add a new server config at runtime and connect it."""
        self._configs[name] = config
        if config.enabled:
            await self._connect(name, config)
        else:
            self._statuses[name] = MCPServerStatus(name=name, status="disabled")

    def status(self) -> list[MCPServerStatus]:
        return list(self._statuses.values())

    async def shutdown(self) -> None:
        """Disconnect all servers."""
        names = list(self._stacks.keys())
        for name in names:
            try:
                await self.disconnect(name)
            except Exception as exc:
                _log.warning(
                    f"mcp server '{name}' shutdown error: {exc}",
                    event="mcp.server.disconnected",
                )