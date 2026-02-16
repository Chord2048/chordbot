"""Prompt template rendering with {{variable}} syntax.

Supports built-in variables (date/time/os/etc.), session context,
config-defined template variables, and caller-provided overrides.
Unknown variables are preserved as-is for safe degradation.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _builtin_variables() -> dict[str, str]:
    """Collect built-in variables that are always available."""
    now = datetime.now()
    utcnow = datetime.now(timezone.utc)

    return {
        # Local time
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        # UTC time
        "date_utc": utcnow.strftime("%Y-%m-%d"),
        "time_utc": utcnow.strftime("%H:%M:%S"),
        "datetime_utc": utcnow.strftime("%Y-%m-%d %H:%M:%S"),
        # Timezone & timestamp
        "timezone": now.astimezone().tzname() or "UTC",
        "unix_timestamp": str(int(time.time())),
        # System info
        "os": platform.system(),
        "os_version": platform.platform(),
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        # cwd (fallback; usually overridden by session_context)
        "cwd": os.getcwd(),
    }


def render_prompt(
    template: str,
    *,
    session_context: dict[str, Any] | None = None,
    extra_variables: dict[str, Any] | None = None,
    template_variables: dict[str, str] | None = None,
) -> str:
    """Render a prompt template by substituting ``{{variable}}`` placeholders.

    Variable resolution order (later wins):
      1. Built-in variables (date, time, os, etc.)
      2. ``template_variables`` (from config prompt_templates)
      3. ``session_context`` (session_id, cwd, worktree, model, agent, …)
      4. ``extra_variables`` (caller-provided explicit overrides)

    Unknown variables are left as-is (e.g. ``{{unknown}}`` stays unchanged).
    """
    if not template:
        return template

    variables: dict[str, str] = _builtin_variables()
    if template_variables:
        variables.update({k: str(v) for k, v in template_variables.items() if v is not None})
    if session_context:
        variables.update({k: str(v) for k, v in session_context.items() if v is not None})
    if extra_variables:
        variables.update({k: str(v) for k, v in extra_variables.items() if v is not None})

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return variables.get(name, match.group(0))  # preserve unknown

    return _VAR_RE.sub(_replace, template)
