"""chordcode sessions — session management commands."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from chordcode.cli.output import Output

sessions_app = typer.Typer(no_args_is_help=True, context_settings={"allow_interspersed_args": True})


@sessions_app.callback()
def _sessions_callback(
    json: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    base_url: str = typer.Option("", "--base-url", help="Server base URL."),
):
    from chordcode.cli.app import _apply_global_opts
    _apply_global_opts(json=json, base_url=base_url)


def _out() -> Output:
    from chordcode.cli.app import state
    return Output(json_mode=state.json_mode)


def _client():
    from chordcode.cli.app import state
    from chordcode.cli.client import Client
    return Client(state.base_url)


@sessions_app.command("list")
def sessions_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Max results."),
    offset: int = typer.Option(0, "--offset", help="Skip first N."),
):
    """List all sessions."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/sessions", params={"limit": limit, "offset": offset}))
        sessions = data.get("sessions", [])
        if out.json_mode:
            out.data(sessions)
        else:
            out.table(
                sessions,
                [("id", "ID"), ("title", "Title"), ("worktree", "Worktree"), ("updated_at", "Updated")],
                title=f"Sessions ({len(sessions)})",
            )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@sessions_app.command("get")
def sessions_get(session_id: str = typer.Argument(..., help="Session ID.")):
    """Get session details."""
    out = _out()
    try:
        data = asyncio.run(_client().get(f"/sessions/{session_id}"))
        out.data(data, title=f"Session {session_id}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@sessions_app.command("create")
def sessions_create(
    worktree: str = typer.Option(..., "--worktree", "-w", help="Absolute path to worktree."),
    title: str = typer.Option("New session", "--title", "-t", help="Session title."),
    cwd: str = typer.Option("", "--cwd", help="Working directory (defaults to worktree)."),
    runtime: str = typer.Option("local", "--runtime", help="Runtime backend: local | daytona."),
    daytona_sandbox_id: str = typer.Option("", "--daytona-sandbox-id", help="Existing Daytona sandbox ID."),
):
    """Create a new session."""
    out = _out()
    try:
        runtime_val = runtime.strip().lower()
        if runtime_val not in ("local", "daytona"):
            raise typer.BadParameter("runtime must be one of: local, daytona")
        payload: dict[str, object] = {"worktree": worktree, "title": title, "cwd": cwd}
        if runtime_val == "daytona":
            payload["runtime"] = {
                "backend": "daytona",
                "daytona": {"sandbox_id": daytona_sandbox_id.strip() or None},
            }
        else:
            payload["runtime"] = {"backend": "local"}
        data = asyncio.run(_client().post("/sessions", json=payload))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Session created: {data.get('id')}")
            out.data(data, title="New Session")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@sessions_app.command("rename")
def sessions_rename(
    session_id: str = typer.Argument(..., help="Session ID."),
    title: str = typer.Option(..., "--title", "-t", help="New title."),
):
    """Rename a session."""
    out = _out()
    try:
        data = asyncio.run(_client().patch(f"/sessions/{session_id}", json={"title": title}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Session renamed: {data.get('title')}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@sessions_app.command("delete")
def sessions_delete(session_id: str = typer.Argument(..., help="Session ID.")):
    """Delete a session."""
    out = _out()
    try:
        data = asyncio.run(_client().delete(f"/sessions/{session_id}"))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Session deleted: {session_id}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@sessions_app.command("messages")
def sessions_messages(session_id: str = typer.Argument(..., help="Session ID.")):
    """List messages for a session."""
    out = _out()
    try:
        data = asyncio.run(_client().get(f"/sessions/{session_id}/messages"))
        if out.json_mode:
            out.data(data)
        else:
            messages = data if isinstance(data, list) else data.get("messages", data)
            if isinstance(messages, list):
                out.table(
                    [_msg_row(m) for m in messages],
                    [("id", "ID"), ("role", "Role"), ("agent", "Agent"), ("created_at", "Created")],
                    title=f"Messages ({len(messages)})",
                )
            else:
                out.data(data, title="Messages")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


def _msg_row(m: dict) -> dict:
    info = m.get("info", m)
    return {
        "id": str(info.get("id", ""))[:12],
        "role": info.get("role", ""),
        "agent": info.get("agent", ""),
        "created_at": info.get("created_at", ""),
    }


@sessions_app.command("todos")
def sessions_todos(session_id: str = typer.Argument(..., help="Session ID.")):
    """Get todo list for a session."""
    out = _out()
    try:
        data = asyncio.run(_client().get(f"/sessions/{session_id}/todos"))
        todos = data.get("todos", [])
        if out.json_mode:
            out.data(todos)
        else:
            out.table(
                todos,
                [("id", "ID"), ("content", "Content"), ("status", "Status"), ("priority", "Priority")],
                title=f"Todos ({len(todos)})",
            )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
