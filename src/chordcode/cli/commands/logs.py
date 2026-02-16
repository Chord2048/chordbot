"""chordcode logs — log inspection commands."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from chordcode.cli.output import Output

logs_app = typer.Typer(no_args_is_help=True)


def _out() -> Output:
    from chordcode.cli.app import state
    return Output(json_mode=state.json_mode)


def _client():
    from chordcode.cli.app import state
    from chordcode.cli.client import Client
    return Client(state.base_url)


@logs_app.command("files")
def logs_files():
    """List available log files."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/logs/files"))
        files = data.get("files", [])
        if out.json_mode:
            out.data(data)
        else:
            out.text(f"Log dir: {data.get('log_dir', '?')}")
            out.table(
                files,
                [("date", "Date"), ("name", "File"), ("size", "Size (bytes)")],
                title=f"Log Files ({len(files)})",
            )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@logs_app.command("view")
def logs_view(
    date: str = typer.Option(..., "--date", "-d", help="Date in YYYY-MM-DD format."),
    level: Optional[str] = typer.Option(None, "--level", "-l", help="Filter by level (DEBUG|INFO|WARNING|ERROR)."),
    event: Optional[str] = typer.Option(None, "--event", "-e", help="Filter by event name (substring)."),
    session_id: Optional[str] = typer.Option(None, "--session-id", "-s", help="Filter by session ID."),
    q: Optional[str] = typer.Option(None, "--q", help="Full-text search."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results."),
    offset: int = typer.Option(0, "--offset", help="Skip first N."),
):
    """View and filter logs for a date."""
    out = _out()
    params: dict = {"date": date, "limit": limit, "offset": offset}
    if level:
        params["level"] = level
    if event:
        params["event"] = event
    if session_id:
        params["session_id"] = session_id
    if q:
        params["q"] = q
    try:
        data = asyncio.run(_client().get("/logs", params=params))
        items = data.get("items", [])
        if out.json_mode:
            out.data(data)
        else:
            out.text(f"Date: {data.get('date')} | Total: {data.get('total')} | Showing: {len(items)}")
            out.table(
                items,
                [("ts", "Timestamp"), ("level", "Level"), ("event", "Event"), ("message", "Message")],
                title="Logs",
            )
            if data.get("has_more"):
                out.info(f"More results available (offset={offset + limit}).")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
