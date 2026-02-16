"""chordcode kb — knowledge base operations."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from chordcode.cli.output import Output

kb_app = typer.Typer(no_args_is_help=True, context_settings={"allow_interspersed_args": True})


@kb_app.callback()
def _kb_callback(
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


@kb_app.command("config")
def kb_config():
    """Show KB configuration status."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/kb/config"))
        out.data(data, title="KB Config")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@kb_app.command("status")
def kb_status():
    """Show KB pipeline status."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/kb/pipeline/status"))
        out.data(data, title="KB Pipeline Status")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@kb_app.command("counts")
def kb_counts():
    """Show KB document status counts."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/kb/status_counts"))
        out.data(data, title="KB Status Counts")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@kb_app.command("query")
def kb_query(
    text: str = typer.Argument(..., help="Query text."),
    top_k: int = typer.Option(10, "--top-k", "-k", help="Max results."),
):
    """Query the knowledge base."""
    out = _out()
    try:
        data = asyncio.run(_client().post("/kb/query", json={"query": text, "top_k": top_k}))
        out.data(data, title="KB Query Results")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@kb_app.command("documents")
def kb_documents(
    page: int = typer.Option(1, "--page", "-p", help="Page number."),
    page_size: int = typer.Option(20, "--page-size", "-n", help="Page size."),
):
    """List KB documents."""
    out = _out()
    try:
        data = asyncio.run(_client().post("/kb/documents/list", json={"page": page, "page_size": page_size}))
        out.data(data, title="KB Documents")
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)


@kb_app.command("upload")
def kb_upload(
    file: str = typer.Argument(..., help="Path to file to upload."),
    use_vlm: bool = typer.Option(False, "--use-vlm", help="Parse with VLM before uploading."),
):
    """Upload a file to the knowledge base."""
    out = _out()
    try:
        data = asyncio.run(_client().upload_file("/kb/documents/upload", file, params={"use_vlm": str(use_vlm).lower()}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Uploaded: {file}")
            out.data(data, title="Upload Result")
    except FileNotFoundError:
        out.error(f"File not found: {file}")
        raise typer.Exit(1)
    except Exception as e:
        out.error(str(e))
        raise typer.Exit(1)
