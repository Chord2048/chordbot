"""chordcode serve / stop — start and stop the FastAPI server."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from chordcode.cli.output import Output

PID_FILE = Path.home() / ".chordcode" / "chordcode.pid"
LOG_FILE = Path.home() / ".chordcode" / "serve.log"


def _read_pid() -> int | None:
    """Return PID from the PID file, or None if missing/invalid."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_alive(pid: int) -> bool:
    """Check whether a process with *pid* is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def serve_cmd(
    port: int = typer.Option(4096, "--port", "-p", help="Listen port."),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="Run as background daemon."),
):
    """Start the Chord Code server."""
    if not daemon:
        import uvicorn

        uvicorn.run(
            "chordcode.api.app:app",
            host=host,
            port=port,
            reload=reload,
        )
        return

    # -- daemon mode --
    from chordcode.cli.app import state

    out = Output(json_mode=state.json_mode)

    cmd = [
        sys.executable, "-m", "uvicorn",
        "chordcode.api.app:app",
        "--host", host,
        "--port", str(port),
    ]
    if reload:
        cmd.append("--reload")

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "a")

    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )

    PID_FILE.write_text(str(proc.pid))
    out.data({
        "status": "started",
        "pid": proc.pid,
        "host": host,
        "port": port,
        "log_file": str(LOG_FILE),
        "pid_file": str(PID_FILE),
    })
    if not state.json_mode:
        out.success(f"Chord Code server started (PID {proc.pid}), listening on {host}:{port}")
        out.info(f"Log: {LOG_FILE}")
        out.info(f"PID file: {PID_FILE}")
        out.info("Use 'chordcode stop' to shut down.")


def stop_cmd():
    """Stop the running Chord Code daemon."""
    from chordcode.cli.app import state

    out = Output(json_mode=state.json_mode)
    pid = _read_pid()

    if pid is None:
        if state.json_mode:
            out.data({"status": "not_running", "pid_file": str(PID_FILE)})
        else:
            out.error("No PID file found — server is not running.")
        raise typer.Exit(code=1)

    if not _is_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        if state.json_mode:
            out.data({"status": "stale", "pid": pid, "pid_file": str(PID_FILE)})
        else:
            out.warning(f"Process {pid} is not running (stale PID file). Cleaning up.")
        raise typer.Exit(code=0)

    out.info(f"Sending SIGTERM to PID {pid} ...")
    os.kill(pid, signal.SIGTERM)

    # Wait up to 5 seconds for graceful shutdown.
    force_killed = False
    for _ in range(50):
        if not _is_alive(pid):
            break
        time.sleep(0.1)
    else:
        out.info(f"Process {pid} did not exit in time, sending SIGKILL ...")
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
        force_killed = True

    PID_FILE.unlink(missing_ok=True)
    sig = "SIGKILL" if force_killed else "SIGTERM"
    if state.json_mode:
        out.data({"status": "stopped", "pid": pid, "signal": sig})
    else:
        out.success(f"Server stopped (PID {pid}, {sig}).")
