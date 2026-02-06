from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger as _logger

import contextvars

_CONFIGURED = False
_HANDLER_IDS: list[int] = []

# Agent-friendly context keys (promoted to top-level in JSONL).
_CONTEXT_KEYS: tuple[str, ...] = (
    "event",
    "session_id",
    "message_id",
    "agent",
    "trace_id",
    "tool_name",
    "tool_call_id",
    "duration_ms",
)

_CTX_VARS: dict[str, contextvars.ContextVar[object | None]] = {
    k: contextvars.ContextVar(f"chordcode_log_{k}", default=None) for k in _CONTEXT_KEYS
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _patch_record(record: dict[str, Any]) -> None:
    extra = record.setdefault("extra", {})
    for k, var in _CTX_VARS.items():
        v = var.get()
        if v is not None and k not in extra:
            extra[k] = v
    for k in _CONTEXT_KEYS:
        extra.setdefault(k, None)
    extra.setdefault("service", "chordcode")
    extra["_console_ctx"] = _console_ctx(extra)
    extra["_jsonl"] = json.dumps(_jsonl_payload(record), ensure_ascii=False, separators=(",", ":"))


def _exception_payload(ex: Any) -> dict[str, Any] | None:
    if not ex:
        return None
    try:
        return {
            "type": getattr(ex.type, "__name__", None),
            "message": str(ex.value) if getattr(ex, "value", None) is not None else None,
            "traceback": "".join(ex.traceback.format()) if getattr(ex, "traceback", None) is not None else None,
        }
    except Exception:
        return {"type": "Unknown", "message": None, "traceback": None}


def _jsonl_payload(record: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = record.get("extra") or {}

    payload: dict[str, Any] = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
        "process": record["process"].id,
        "thread": record["thread"].id,
        "service": extra.get("service", "chordcode"),
    }

    for k in _CONTEXT_KEYS:
        v = extra.get(k)
        if v is not None and v != "":
            payload[k] = v

    ex = _exception_payload(record.get("exception"))
    if ex:
        payload["exception"] = ex

    # Preserve other extra values without polluting the top-level namespace.
    known = set(_CONTEXT_KEYS) | {"service"}
    other_extra = {k: v for k, v in extra.items() if k not in known and v is not None}
    other_extra = {k: v for k, v in other_extra.items() if not str(k).startswith("_")}
    if other_extra:
        payload["extra"] = other_extra

    return payload


def _console_ctx(extra: dict[str, Any]) -> str:
    event = extra.get("event")
    session_id = extra.get("session_id")
    message_id = extra.get("message_id")
    trace_id = extra.get("trace_id")
    tool_name = extra.get("tool_name")
    duration_ms = extra.get("duration_ms")

    parts: list[str] = []
    if event:
        parts.append(f"<cyan>{event}</cyan>")
    if session_id:
        parts.append(f"sid={session_id}")
    if message_id:
        parts.append(f"mid={message_id}")
    if trace_id:
        parts.append(f"trace={trace_id}")
    if tool_name:
        parts.append(f"tool={tool_name}")
    if isinstance(duration_ms, (int, float)):
        parts.append(f"{duration_ms:.1f}ms")
    return (" | " + " ".join(parts)) if parts else ""


def init_logging(*, force: bool = False) -> None:
    """
    Configure Loguru sinks:
    - Console: human-friendly + colored
    - File: JSONL (one JSON object per line), rotated daily, retained for 7 days

    Config via env vars (defaults shown):
    - CHORDCODE_LOG_LEVEL=INFO
    - CHORDCODE_LOG_CONSOLE=true
    - CHORDCODE_LOG_FILE=true
    - CHORDCODE_LOG_DIR=./data/logs
    - CHORDCODE_LOG_ROTATION=00:00
    - CHORDCODE_LOG_RETENTION=7 days
    """
    global _CONFIGURED, _HANDLER_IDS
    if _CONFIGURED and not force:
        return

    level = _env_str("CHORDCODE_LOG_LEVEL", "INFO") or "INFO"
    enable_console = _env_bool("CHORDCODE_LOG_CONSOLE", True)
    enable_file = _env_bool("CHORDCODE_LOG_FILE", True)
    log_dir = Path(_env_str("CHORDCODE_LOG_DIR", "./data/logs") or "./data/logs")
    rotation = _env_str("CHORDCODE_LOG_ROTATION", "00:00") or "00:00"
    retention = _env_str("CHORDCODE_LOG_RETENTION", "7 days") or "7 days"

    _logger.remove()
    _logger.configure(patcher=_patch_record)

    _HANDLER_IDS = []
    if enable_console:
        _HANDLER_IDS.append(
            _logger.add(
                sys.stderr,
                level=level,
                format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level>{extra[_console_ctx]} - <level>{message}</level>\n{exception}",
                colorize=True,
                enqueue=True,
                backtrace=False,
                diagnose=False,
            ),
        )

    if enable_file:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "chordcode_{time:YYYY-MM-DD}.jsonl"
        _HANDLER_IDS.append(
            _logger.add(
                str(path),
                level=level,
                format="{extra[_jsonl]}\n",
                rotation=rotation,
                retention=retention,
                encoding="utf-8",
                enqueue=True,
                backtrace=False,
                diagnose=False,
            ),
        )

    _logger.enable("chordcode")
    _CONFIGURED = True


def shutdown_logging() -> None:
    global _CONFIGURED, _HANDLER_IDS
    for hid in _HANDLER_IDS:
        try:
            _logger.remove(hid)
        except Exception:
            pass
    _HANDLER_IDS = []
    _CONFIGURED = False
    _logger.disable("chordcode")


@contextmanager
def log_context(**kwargs: object):
    tokens: list[tuple[contextvars.ContextVar[object | None], contextvars.Token[object | None]]] = []
    for k, v in kwargs.items():
        var = _CTX_VARS.get(k)
        if var is None:
            continue
        tokens.append((var, var.set(v)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


log = _logger

# Keep library output quiet by default; enable after init_logging() so callers
# don't get noisy logs when importing modules in tests/scripts.
_logger.disable("chordcode")
