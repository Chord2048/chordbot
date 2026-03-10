from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryFileRecord:
    path: str
    hash: str
    mtime_ms: int
    size: int


@dataclass(frozen=True)
class MemoryChunk:
    id: str
    path: str
    source: str
    start_line: int
    end_line: int
    hash: str
    text: str


@dataclass(frozen=True)
class MemoryHit:
    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    source: str

