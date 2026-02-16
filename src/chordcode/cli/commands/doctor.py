"""chordcode doctor — validate setup and system health."""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer

from chordcode.cli.output import Output


def _check_python() -> dict[str, Any]:
    v = sys.version_info
    ok = v >= (3, 11)
    return {"name": "Python version", "ok": ok, "detail": f"{v.major}.{v.minor}.{v.micro}", "hint": "Requires Python 3.11+" if not ok else ""}


def _check_config() -> dict[str, Any]:
    try:
        from chordcode.config import load
        cfg = load()
        return {"name": "Config loadable", "ok": True, "detail": "loaded successfully"}
    except Exception as e:
        return {"name": "Config loadable", "ok": False, "detail": str(e), "hint": "Run `chordcode config init` to create a default config."}


def _check_required_fields() -> dict[str, Any]:
    try:
        from chordcode.config import load
        cfg = load()
        missing = []
        if not cfg.openai.base_url:
            missing.append("openai.base_url")
        if not cfg.openai.api_key:
            missing.append("openai.api_key")
        if not cfg.openai.model:
            missing.append("openai.model")
        if missing:
            return {"name": "Required config fields", "ok": False, "detail": f"missing: {', '.join(missing)}", "hint": "Edit config.yaml to set these fields."}
        return {"name": "Required config fields", "ok": True, "detail": f"model={cfg.openai.model}"}
    except Exception as e:
        return {"name": "Required config fields", "ok": False, "detail": str(e)}


async def _check_server(base_url: str) -> dict[str, Any]:
    from chordcode.cli.client import Client
    c = Client(base_url)
    reachable = await c.ping()
    if reachable:
        return {"name": "Server reachable", "ok": True, "detail": base_url}
    return {"name": "Server reachable", "ok": False, "detail": base_url, "hint": "Start with `chordcode serve`."}


async def _check_api_health(base_url: str) -> list[dict[str, Any]]:
    """Run API-dependent checks (requires server)."""
    from chordcode.cli.client import Client, APIError
    results: list[dict[str, Any]] = []
    c = Client(base_url)

    # DB check via sessions list
    try:
        await c.get("/sessions", params={"limit": 1})
        results.append({"name": "Database", "ok": True, "detail": "sessions query OK"})
    except APIError as e:
        results.append({"name": "Database", "ok": False, "detail": e.detail})
    except Exception as e:
        results.append({"name": "Database", "ok": False, "detail": str(e)})

    # MCP status
    try:
        data = await c.get("/mcp/status")
        servers = data.get("servers", [])
        connected = sum(1 for s in servers if s.get("status") == "connected")
        results.append({"name": "MCP servers", "ok": True, "detail": f"{connected}/{len(servers)} connected"})
    except Exception as e:
        results.append({"name": "MCP servers", "ok": False, "detail": str(e)})

    # Skills
    try:
        data = await c.get("/skills")
        skills = data.get("skills", [])
        results.append({"name": "Skills", "ok": True, "detail": f"{len(skills)} discovered"})
    except Exception as e:
        results.append({"name": "Skills", "ok": False, "detail": str(e)})

    # KB
    try:
        data = await c.get("/kb/config")
        enabled = data.get("enabled", False)
        results.append({"name": "Knowledge Base", "ok": True, "detail": f"enabled={enabled}, backend={data.get('backend', 'none')}"})
    except Exception as e:
        results.append({"name": "Knowledge Base", "ok": False, "detail": str(e)})

    return results


def doctor_cmd():
    """Validate setup and system health."""
    from chordcode.cli.app import state

    out = Output(json_mode=state.json_mode)
    results: list[dict[str, Any]] = []

    # Offline checks (no server needed)
    results.append(_check_python())
    results.append(_check_config())
    results.append(_check_required_fields())

    # Server check
    server_result = asyncio.run(_check_server(state.base_url))
    results.append(server_result)

    # Online checks (require server)
    if server_result["ok"]:
        api_results = asyncio.run(_check_api_health(state.base_url))
        results.extend(api_results)
    else:
        for name in ("Database", "MCP servers", "Skills", "Knowledge Base"):
            results.append({"name": name, "ok": False, "detail": "skipped (server not reachable)"})

    # Worktree check
    try:
        from chordcode.config import load
        cfg = load()
        import os
        wt = cfg.default_worktree
        if wt and os.path.isdir(wt):
            results.append({"name": "Worktree", "ok": True, "detail": wt})
        elif wt:
            results.append({"name": "Worktree", "ok": False, "detail": f"not a directory: {wt}"})
        else:
            results.append({"name": "Worktree", "ok": True, "detail": "(auto-detect)"})
    except Exception as e:
        results.append({"name": "Worktree", "ok": False, "detail": str(e)})

    # Output
    if state.json_mode:
        all_ok = all(r["ok"] for r in results)
        out.data({"healthy": all_ok, "checks": results})
    else:
        for r in results:
            icon = "[green]PASS[/green]" if r["ok"] else "[red]FAIL[/red]"
            out.text(f"  {icon}  {r['name']}: {r.get('detail', '')}")
            hint = r.get("hint")
            if hint and not r["ok"]:
                out.text(f"         [dim]{hint}[/dim]")
        all_ok = all(r["ok"] for r in results)
        if all_ok:
            out.success("All checks passed.")
        else:
            failed = sum(1 for r in results if not r["ok"])
            out.error(f"{failed} check(s) failed.")

    if not all(r["ok"] for r in results):
        raise typer.Exit(1)
