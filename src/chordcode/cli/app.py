"""Chord Code CLI — typer root application with global options."""
from __future__ import annotations

from typing import Optional

import typer

import chordcode

app = typer.Typer(
    name="chordcode",
    help="Chord Code CLI — local-first Agent Core.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# ── Global state shared across sub-commands ──────────────────────────
class _State:
    json_mode: bool = False
    base_url: str = "http://127.0.0.1:4096"

state = _State()


def _version_callback(value: bool):
    if value:
        typer.echo(f"chordcode {chordcode.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    json: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    base_url: str = typer.Option("http://127.0.0.1:4096", "--base-url", envvar="CHORDCODE_URL", help="Server base URL."),
    version: Optional[bool] = typer.Option(None, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."),
):
    """Chord Code CLI — local-first Agent Core."""
    state.json_mode = json
    state.base_url = base_url.rstrip("/")


# ── Register sub-command groups ──────────────────────────────────────
from chordcode.cli.commands.serve import serve_cmd, stop_cmd  # noqa: E402
from chordcode.cli.commands.doctor import doctor_cmd        # noqa: E402
from chordcode.cli.commands.config import config_app        # noqa: E402
from chordcode.cli.commands.sessions import sessions_app    # noqa: E402
from chordcode.cli.commands.logs import logs_app            # noqa: E402
from chordcode.cli.commands.permissions import perm_app     # noqa: E402
from chordcode.cli.commands.skills import skills_app        # noqa: E402
from chordcode.cli.commands.mcp import mcp_app              # noqa: E402
from chordcode.cli.commands.kb import kb_app                # noqa: E402
from chordcode.cli.commands.agent import agent_app          # noqa: E402
from chordcode.cli.commands.run import run_cmd              # noqa: E402

app.command("serve")(serve_cmd)
app.command("stop")(stop_cmd)
app.command("doctor")(doctor_cmd)
app.command("run")(run_cmd)
app.add_typer(config_app, name="config", help="Configuration management.")
app.add_typer(sessions_app, name="sessions", help="Session management.")
app.add_typer(logs_app, name="logs", help="Log inspection.")
app.add_typer(perm_app, name="permissions", help="Permission management.")
app.add_typer(skills_app, name="skills", help="Skills discovery.")
app.add_typer(mcp_app, name="mcp", help="MCP server management.")
app.add_typer(kb_app, name="kb", help="Knowledge base operations.")
app.add_typer(agent_app, name="agent", help="Agent message & execution.")
