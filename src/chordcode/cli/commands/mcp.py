"""chordcode mcp — MCP server management commands."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from chordcode.cli.output import Output

mcp_app = typer.Typer(no_args_is_help=True)


def _out() -> Output:
    from chordcode.cli.app import state
    return Output(json_mode=state.json_mode)


def _client():
    from chordcode.cli.app import state
    from chordcode.cli.client import Client
    return Client(state.base_url)


@mcp_app.command("status")
def mcp_status():
    """Show MCP server statuses."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/mcp/status"))
        servers = data.get("servers", [])
        if out.json_mode:
            out.data(data)
        else:
            out.table(
                servers,
                [("name", "Name"), ("status", "Status"), ("tool_count", "Tools"), ("error", "Error")],
                title=f"MCP Servers ({len(servers)})",
            )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@mcp_app.command("tools")
def mcp_tools():
    """List all MCP tools."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/mcp/tools"))
        tools = data.get("tools", [])
        if out.json_mode:
            out.data(data)
        else:
            out.table(
                tools,
                [("server", "Server"), ("namespaced_name", "Name"), ("description", "Description")],
                title=f"MCP Tools ({len(tools)})",
            )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@mcp_app.command("connect")
def mcp_connect(name: str = typer.Argument(..., help="MCP server name.")):
    """Connect to an MCP server."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/mcp/{name}/connect"))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Connected to MCP server: {name}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@mcp_app.command("disconnect")
def mcp_disconnect(name: str = typer.Argument(..., help="MCP server name.")):
    """Disconnect from an MCP server."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/mcp/{name}/disconnect"))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Disconnected from MCP server: {name}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@mcp_app.command("add")
def mcp_add(
    name: str = typer.Option(..., "--name", help="Server name."),
    command: Optional[str] = typer.Option(None, "--command", help="Local server command."),
    url: Optional[str] = typer.Option(None, "--url", help="Remote server URL."),
    args: Optional[str] = typer.Option(None, "--args", help="Comma-separated arguments for command."),
):
    """Add a new MCP server dynamically."""
    out = _out()
    if not command and not url:
        out.error("Must provide --command or --url.")
        raise typer.Exit(1)
    config: dict = {}
    if command:
        config["command"] = command
        if args:
            config["args"] = [a.strip() for a in args.split(",")]
    else:
        config["url"] = url
    try:
        data = asyncio.run(_client().post("/mcp/servers", json={"name": name, "config": config}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Added MCP server: {name}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
