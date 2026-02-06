from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Truncated:
    content: str
    truncated: bool


def truncate(text: str, *, max_chars: int = 200_000) -> Truncated:
    if len(text) <= max_chars:
        return Truncated(content=text, truncated=False)
    return Truncated(content=text[:max_chars] + "\n\n... (truncated)", truncated=True)

