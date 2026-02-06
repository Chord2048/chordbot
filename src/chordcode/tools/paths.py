from __future__ import annotations

from pathlib import Path


def resolve_path(*, cwd: str, file_path: str) -> str:
    p = Path(file_path)
    if p.is_absolute():
        return str(p)
    return str((Path(cwd) / p).resolve())


def is_within(*, root: str, path: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False

