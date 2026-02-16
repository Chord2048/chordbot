"""chordcode config — configuration management commands."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from chordcode.cli.output import Output

config_app = typer.Typer(no_args_is_help=True)


def _out() -> Output:
    from chordcode.cli.app import state
    return Output(json_mode=state.json_mode)


def _client():
    from chordcode.cli.app import state
    from chordcode.cli.client import Client
    return Client(state.base_url)


@config_app.command("show")
def config_show():
    """Show merged config (sensitive values masked)."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/config"))
        out.data(data, title="Config (merged, masked)")
    except Exception as e:
        out.error(str(e), hint="Is the server running? Start with `chordcode serve`.")
        raise typer.Exit(1)


@config_app.command("schema")
def config_schema():
    """Show config field metadata."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/config/schema"))
        out.data(data, title="Config Schema")
    except Exception as e:
        out.error(str(e), hint="Is the server running?")
        raise typer.Exit(1)


@config_app.command("sources")
def config_sources():
    """Show config file paths and discovery status."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/config/sources"))
        sources = data.get("sources", [])
        if out.json_mode:
            out.data(sources)
        else:
            out.table(
                sources,
                [("path", "Path"), ("exists", "Exists"), ("scope", "Scope")],
                title="Config Sources",
            )
    except Exception as e:
        out.error(str(e), hint="Is the server running?")
        raise typer.Exit(1)


@config_app.command("raw")
def config_raw(
    scope: str = typer.Option("project", "--scope", "-s", help="project or global."),
):
    """Show raw config file content."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/config/raw", params={"scope": scope}))
        if out.json_mode:
            out.data(data)
        else:
            content = data.get("content", "")
            path = data.get("path", "")
            exists = data.get("exists", False)
            if not exists:
                out.warning(f"No config file at: {path}")
            else:
                out.text(f"# {path}\n{content}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@config_app.command("init")
def config_init(
    scope: str = typer.Option("project", "--scope", "-s", help="project or global."),
):
    """Generate a default config file."""
    out = _out()
    try:
        data = asyncio.run(_client().post("/config/init", json={"scope": scope}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Config created at: {data.get('path')}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
