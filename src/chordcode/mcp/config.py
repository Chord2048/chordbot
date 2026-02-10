from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from chordcode.log import logger

_log = logger.child(service="mcp")

GLOBAL_SCAN_PATHS: tuple[str, ...] = (
    "~/.cursor/mcp.json",
    "~/.chordcode/mcp.json",
)


def project_scan_paths(worktree: str) -> tuple[str, ...]:
    return (
        f"{worktree}/.cursor/mcp.json",
        f"{worktree}/.chordcode/mcp.json",
    )


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    type: Literal["local", "remote"]
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: int = 30
    source: str = ""
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio"


def _parse_server_entry(
    name: str, raw: dict[str, Any], source: str,
) -> MCPServerConfig | None:
    """Parse a single server entry from mcp.json. Returns None on invalid."""
    if not isinstance(raw, dict):
        _log.warning(
            f"mcp config: server '{name}' is not a dict, skipping",
            event="mcp.config.skip",
        )
        return None

    enabled = raw.get("enabled", True)
    if not enabled:
        return MCPServerConfig(
            name=name, type="local", enabled=False, source=source,
        )

    timeout = int(raw.get("timeout", 30))

    # Determine type: command → local (stdio), url → remote
    command = raw.get("command", "")
    url = raw.get("url", "")

    if command:
        return MCPServerConfig(
            name=name,
            type="local",
            command=command,
            args=list(raw.get("args", [])),
            env=dict(raw.get("env", {})),
            enabled=True,
            timeout=timeout,
            source=source,
            transport="stdio",
        )

    if url:
        explicit_transport = raw.get("transport", "")
        if explicit_transport == "sse":
            transport: Literal["stdio", "sse", "streamable-http"] = "sse"
        else:
            transport = "streamable-http"
        return MCPServerConfig(
            name=name,
            type="remote",
            url=url,
            headers=dict(raw.get("headers", {})),
            enabled=True,
            timeout=timeout,
            source=source,
            transport=transport,
        )

    _log.warning(
        f"mcp config: server '{name}' has neither 'command' nor 'url', skipping",
        event="mcp.config.skip",
    )
    return None


def _load_file(path: str) -> dict[str, Any] | None:
    """Load and parse a single mcp.json file. Returns None on error."""
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            _log.warning(
                f"mcp config: {p} root is not a dict, skipping",
                event="mcp.config.skip",
            )
            return None
        return data
    except json.JSONDecodeError as exc:
        _log.warning(
            f"mcp config: {p} invalid JSON: {exc}",
            event="mcp.config.skip",
        )
        return None
    except OSError as exc:
        _log.warning(
            f"mcp config: {p} read error: {exc}",
            event="mcp.config.skip",
        )
        return None


def load_mcp_configs(worktree: str) -> dict[str, MCPServerConfig]:
    """Scan global + project paths, return merged configs (later overrides earlier)."""
    all_paths = list(GLOBAL_SCAN_PATHS) + list(project_scan_paths(worktree))
    configs: dict[str, MCPServerConfig] = {}

    for path in all_paths:
        data = _load_file(path)
        if data is None:
            continue
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            _log.warning(
                f"mcp config: {path} 'mcpServers' is not a dict, skipping",
                event="mcp.config.skip",
            )
            continue
        for name, entry in servers.items():
            cfg = _parse_server_entry(name, entry, source=path)
            if cfg is not None:
                configs[name] = cfg

    _log.info(
        f"mcp config: loaded {len(configs)} server(s)",
        event="mcp.config.loaded",
    )
    return configs
