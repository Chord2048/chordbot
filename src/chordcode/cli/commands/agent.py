"""chordcode agent — send messages, run agent loop, interrupt."""
from __future__ import annotations

import asyncio

import typer

from chordcode.cli.output import Output

agent_app = typer.Typer(no_args_is_help=True, context_settings={"allow_interspersed_args": True})


@agent_app.callback()
def _agent_callback(
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


@agent_app.command("send")
def agent_send(
    session_id: str = typer.Argument(..., help="Session ID."),
    text: str = typer.Argument(..., help="User message text."),
):
    """Add a user message to a session."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/sessions/{session_id}/messages", json={"text": text}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Message sent: {data.get('message_id', '')[:12]}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@agent_app.command("run")
def agent_run(session_id: str = typer.Argument(..., help="Session ID.")):
    """Trigger agent loop on a session."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/sessions/{session_id}/run"))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Agent run complete. Message: {data.get('assistant_message_id', '')[:12]}")
            trace_url = data.get("trace_url")
            if trace_url:
                out.info(f"Trace: {trace_url}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@agent_app.command("interrupt")
def agent_interrupt(session_id: str = typer.Argument(..., help="Session ID.")):
    """Interrupt a running session."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/sessions/{session_id}/interrupt"))
        if out.json_mode:
            out.data(data)
        else:
            out.success("Session interrupted.")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
