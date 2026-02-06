from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from chordcode.model import PermissionRule


@dataclass(frozen=True)
class Decision:
    action: str


def evaluate_permission(permission: str, pattern: str, rules: list[PermissionRule]) -> Decision:
    """
    Evaluate one permission pattern against rules.

    The first matching rule wins, matching current PermissionService behavior.
    """
    for r in rules:
        if r.permission != permission and r.permission != "*":
            continue
        if fnmatch(pattern, r.pattern):
            return Decision(action=r.action)
    return Decision(action="ask")
