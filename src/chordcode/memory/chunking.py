from __future__ import annotations

import hashlib

from chordcode.memory.types import MemoryChunk


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chunk_markdown(
    *,
    path: str,
    content: str,
    source: str = "memory",
    max_chars: int = 1000,
    overlap_chars: int = 200,
) -> list[MemoryChunk]:
    lines = content.split("\n")
    if not lines:
        return []

    chunks: list[MemoryChunk] = []
    window: list[tuple[int, str]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal window, current_chars
        if not window:
            return
        start_line = window[0][0]
        end_line = window[-1][0]
        text = "\n".join(line for _, line in window)
        chunk_hash = hash_text(text)
        chunk_id = hash_text(f"{path}:{start_line}:{end_line}:{chunk_hash}")
        chunks.append(
            MemoryChunk(
                id=chunk_id,
                path=path,
                source=source,
                start_line=start_line,
                end_line=end_line,
                hash=chunk_hash,
                text=text,
            )
        )

        if overlap_chars <= 0:
            window = []
            current_chars = 0
            return

        kept: list[tuple[int, str]] = []
        kept_chars = 0
        for entry in reversed(window):
            kept.insert(0, entry)
            kept_chars += len(entry[1]) + 1
            if kept_chars >= overlap_chars:
                break
        window = kept
        current_chars = kept_chars

    for index, line in enumerate(lines, start=1):
        segments: list[str]
        if not line:
            segments = [""]
        else:
            segments = [line[pos : pos + max_chars] for pos in range(0, len(line), max_chars)]
        for segment in segments:
            size = len(segment) + 1
            if current_chars + size > max_chars and window:
                flush()
            window.append((index, segment))
            current_chars += size

    if window:
        flush()

    return chunks

