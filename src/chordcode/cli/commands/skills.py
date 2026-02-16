"""chordcode skills — skills discovery commands."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from chordcode.cli.output import Output

skills_app = typer.Typer(no_args_is_help=True, context_settings={"allow_interspersed_args": True})


@skills_app.callback()
def _skills_callback(
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


@skills_app.command("list")
def skills_list(
    worktree: Optional[str] = typer.Option(None, "--worktree", "-w", help="Worktree path to scan."),
):
    """List available skills."""
    out = _out()
    params: dict = {}
    if worktree:
        params["worktree"] = worktree
    try:
        data = asyncio.run(_client().get("/skills", params=params))
        skills = data.get("skills", [])
        if out.json_mode:
            out.data(data)
        else:
            out.table(
                skills,
                [("name", "Name"), ("description", "Description"), ("path", "Path")],
                title=f"Skills ({len(skills)})",
            )
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@skills_app.command("get")
def skills_get(
    name: str = typer.Argument(..., help="Skill name."),
    worktree: Optional[str] = typer.Option(None, "--worktree", "-w", help="Worktree path."),
):
    """Get skill details including body and files."""
    out = _out()
    params: dict = {}
    if worktree:
        params["worktree"] = worktree
    try:
        data = asyncio.run(_client().get(f"/skills/{name}", params=params))
        out.data(data, title=f"Skill: {name}")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
