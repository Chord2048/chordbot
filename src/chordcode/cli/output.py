"""Output helpers — JSON vs rich formatting controlled by --json flag."""
from __future__ import annotations

import json
import sys
from typing import Any, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint


_console = Console(stderr=True)
_stdout = Console()


class Output:
    """Unified output: JSON to stdout when json_mode, rich otherwise."""

    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    # ── Data output (stdout) ─────────────────────────────────────────

    def data(self, obj: Any, *, title: str | None = None) -> None:
        """Print a dict / any JSON-serializable object."""
        if self.json_mode:
            print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        else:
            formatted = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
            _stdout.print(Panel(formatted, title=title, border_style="blue"))

    def table(
        self,
        rows: Sequence[dict[str, Any]],
        columns: Sequence[tuple[str, str]],  # (key, header)
        *,
        title: str | None = None,
    ) -> None:
        """Print a list of dicts as a table."""
        if self.json_mode:
            print(json.dumps(list(rows), ensure_ascii=False, indent=2, default=str))
            return
        t = Table(title=title, show_lines=False)
        for _, header in columns:
            t.add_column(header)
        for row in rows:
            t.add_row(*(str(row.get(k, "")) for k, _ in columns))
        _stdout.print(t)

    def text(self, text: str) -> None:
        """Print plain text to stdout."""
        if self.json_mode:
            print(json.dumps({"text": text}, ensure_ascii=False))
        else:
            _stdout.print(text)

    # ── Status output (stderr) ───────────────────────────────────────

    def success(self, msg: str) -> None:
        if self.json_mode:
            print(json.dumps({"status": "ok", "message": msg}), file=sys.stderr)
        else:
            _console.print(f"[green]OK[/green] {msg}")

    def error(self, msg: str, *, hint: str | None = None) -> None:
        if self.json_mode:
            out: dict[str, str] = {"status": "error", "message": msg}
            if hint:
                out["hint"] = hint
            print(json.dumps(out), file=sys.stderr)
        else:
            _console.print(f"[red]Error:[/red] {msg}")
            if hint:
                _console.print(f"  [dim]{hint}[/dim]")

    def warning(self, msg: str) -> None:
        if self.json_mode:
            print(json.dumps({"status": "warning", "message": msg}), file=sys.stderr)
        else:
            _console.print(f"[yellow]Warning:[/yellow] {msg}")

    def info(self, msg: str) -> None:
        if self.json_mode:
            pass  # suppress info in JSON mode
        else:
            _console.print(f"[dim]{msg}[/dim]")
