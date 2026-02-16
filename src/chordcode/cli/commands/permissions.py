"""chordcode permissions — permission management commands."""
from __future__ import annotations

import asyncio

import typer

from chordcode.cli.output import Output

perm_app = typer.Typer(no_args_is_help=True)


def _out() -> Output:
    from chordcode.cli.app import state
    return Output(json_mode=state.json_mode)


def _client():
    from chordcode.cli.app import state
    from chordcode.cli.client import Client
    return Client(state.base_url)


@perm_app.command("pending")
def perm_pending(
    session_id: str = typer.Option(..., "--session-id", "-s", help="Session ID."),
):
    """List pending permission requests for a session."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/permissions/pending", params={"session_id": session_id}))
        if out.json_mode:
            out.data(data)
        else:
            items = data if isinstance(data, list) else [data]
            if not items or (len(items) == 1 and not items[0]):
                out.info("No pending permission requests.")
            else:
                out.table(
                    items,
                    [("id", "ID"), ("permission", "Permission"), ("patterns", "Patterns"), ("tool", "Tool")],
                    title="Pending Permissions",
                )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@perm_app.command("reply")
def perm_reply(
    request_id: str = typer.Argument(..., help="Permission request ID."),
    action: str = typer.Option("once", "--action", "-a", help="once | always | reject"),
    message: str = typer.Option("", "--message", "-m", help="Optional message."),
):
    """Reply to a pending permission request."""
    out = _out()
    if action not in ("once", "always", "reject"):
        out.error("action must be one of: once, always, reject")
        raise typer.Exit(1)
    body: dict = {"reply": action}
    if message:
        body["message"] = message
    try:
        data = asyncio.run(_client().post(f"/permissions/{request_id}/reply", json=body))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Permission {request_id}: {action}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
