"""MCP (Model Context Protocol) client support.

Discovers MCP server configs from global/project mcp.json files, manages
server connections (stdio for local, streamable-http/SSE for remote), and
exposes MCP tools to the LLM via MCPToolAdapter (satisfies the Tool Protocol).

Modules:
    config        -- MCPServerConfig dataclass + load_mcp_configs() scanner.
    client        -- MCPManager: connection lifecycle, tool caching, call_tool().
    tool_adapter  -- MCPToolAdapter: wraps MCP tools as built-in Tool instances.

Usage in app.py:
    configs = load_mcp_configs(worktree)
    await mcp_manager.initialize(configs)          # startup
    mcp_tools = [MCPToolAdapter(t, mcp_manager) for t in await mcp_manager.list_tools()]
    tools = ToolRegistry(builtin_tools + mcp_tools) # per-request
    await mcp_manager.shutdown()                    # shutdown
"""

from chordcode.mcp.client import MCPCallResult, MCPManager, MCPServerStatus, MCPToolInfo
from chordcode.mcp.config import MCPServerConfig, load_mcp_configs
from chordcode.mcp.tool_adapter import MCPToolAdapter

__all__ = [
    "MCPCallResult",
    "MCPManager",
    "MCPServerConfig",
    "MCPServerStatus",
    "MCPToolAdapter",
    "MCPToolInfo",
    "load_mcp_configs",
]
