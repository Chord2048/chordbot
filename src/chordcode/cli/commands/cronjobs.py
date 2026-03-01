"""chordcode cronjobs — scheduled agent task commands."""
from __future__ import annotations

import asyncio

import typer

from chordcode.cli.output import Output

cronjobs_app = typer.Typer(no_args_is_help=True, context_settings={"allow_interspersed_args": True})


@cronjobs_app.callback()
def _cronjobs_callback(
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


def _schedule_payload(kind: str, at_ms: int | None, every_ms: int | None, expr: str, tz: str) -> dict[str, object]:
    payload: dict[str, object] = {"kind": kind.strip().lower()}
    if at_ms is not None:
        payload["at_ms"] = at_ms
    if every_ms is not None:
        payload["every_ms"] = every_ms
    if expr.strip():
        payload["expr"] = expr.strip()
    if tz.strip():
        payload["tz"] = tz.strip()
    return payload


@cronjobs_app.command("list")
def cronjobs_list(
    session_id: str = typer.Option("", "--session-id", "-s", help="Filter by session ID."),
    include_disabled: bool = typer.Option(True, "--include-disabled/--enabled-only", help="Include disabled jobs."),
):
    """List cron jobs."""
    out = _out()
    try:
        data = asyncio.run(
            _client().get(
                "/cronjobs",
                params={"session_id": session_id or None, "include_disabled": include_disabled},
            )
        )
        jobs = data.get("jobs", [])
        if out.json_mode:
            out.data(jobs)
        else:
            out.table(
                [_job_row(x) for x in jobs],
                [("id", "ID"), ("name", "Name"), ("session_id", "Session"), ("enabled", "Enabled"), ("next_run_at_ms", "Next Run")],
                title=f"Cron Jobs ({len(jobs)})",
            )
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("get")
def cronjobs_get(job_id: str = typer.Argument(..., help="Cron job ID.")):
    """Get cron job details."""
    out = _out()
    try:
        data = asyncio.run(_client().get(f"/cronjobs/{job_id}"))
        out.data(data, title=f"Cron Job {job_id}")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("create")
def cronjobs_create(
    session_id: str = typer.Option(..., "--session-id", "-s", help="Target session ID."),
    name: str = typer.Option(..., "--name", "-n", help="Job name."),
    message: str = typer.Option(..., "--message", "-m", help="Injected user message."),
    kind: str = typer.Option("every", "--kind", help="Schedule kind: at | every | cron."),
    at_ms: int | None = typer.Option(None, "--at-ms", help="Run once at timestamp (ms)."),
    every_ms: int | None = typer.Option(None, "--every-ms", help="Run every N milliseconds."),
    expr: str = typer.Option("", "--expr", help="Cron expression."),
    tz: str = typer.Option("", "--tz", help="Timezone for cron expression."),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="Start enabled."),
    delete_after_run: bool = typer.Option(False, "--delete-after-run", help="Delete one-shot job after execution."),
):
    """Create a cron job."""
    out = _out()
    body = {
        "session_id": session_id,
        "name": name,
        "message": message,
        "schedule": _schedule_payload(kind.strip(), at_ms, every_ms, expr, tz),
        "enabled": enabled,
        "delete_after_run": delete_after_run,
    }
    try:
        data = asyncio.run(_client().post("/cronjobs", json=body))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Cron job created: {data.get('id', '')}")
            out.data(data, title="Cron Job")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("delete")
def cronjobs_delete(job_id: str = typer.Argument(..., help="Cron job ID.")):
    """Delete a cron job."""
    out = _out()
    try:
        data = asyncio.run(_client().delete(f"/cronjobs/{job_id}"))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Cron job deleted: {job_id}")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("enable")
def cronjobs_enable(job_id: str = typer.Argument(..., help="Cron job ID.")):
    """Enable a cron job."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/cronjobs/{job_id}/enabled", json={"enabled": True}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Cron job enabled: {job_id}")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("disable")
def cronjobs_disable(job_id: str = typer.Argument(..., help="Cron job ID.")):
    """Disable a cron job."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/cronjobs/{job_id}/enabled", json={"enabled": False}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Cron job disabled: {job_id}")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("run")
def cronjobs_run(
    job_id: str = typer.Argument(..., help="Cron job ID."),
    force: bool = typer.Option(False, "--force", help="Run even when job is disabled."),
):
    """Run a cron job immediately."""
    out = _out()
    try:
        data = asyncio.run(_client().post(f"/cronjobs/{job_id}/run", json={"force": force}))
        if out.json_mode:
            out.data(data)
        else:
            out.success(f"Cron job triggered: {job_id}")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("runs")
def cronjobs_runs(
    job_id: str = typer.Argument(..., help="Cron job ID."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max run records."),
):
    """List run history for a cron job."""
    out = _out()
    try:
        data = asyncio.run(_client().get(f"/cronjobs/{job_id}/runs", params={"limit": limit}))
        runs = data.get("runs", [])
        if out.json_mode:
            out.data(runs)
        else:
            out.table(
                runs,
                [("id", "ID"), ("status", "Status"), ("started_at_ms", "Started"), ("finished_at_ms", "Finished"), ("error", "Error")],
                title=f"Cron Runs ({len(runs)})",
            )
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


@cronjobs_app.command("status")
def cronjobs_status():
    """Show cron service status."""
    out = _out()
    try:
        data = asyncio.run(_client().get("/cronjobs/status"))
        out.data(data, title="Cron Service")
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(1)


def _job_row(job: dict) -> dict[str, object]:
    state = job.get("state", {}) if isinstance(job, dict) else {}
    return {
        "id": str(job.get("id", ""))[:12],
        "name": job.get("name", ""),
        "session_id": str(job.get("session_id", ""))[:12],
        "enabled": job.get("enabled", False),
        "next_run_at_ms": state.get("next_run_at_ms"),
    }
