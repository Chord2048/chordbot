from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from chordcode.model import MessageWithParts, Session

_ARCHIVE_MARKER_PREFIX = "<!-- session-archive:"
_MAX_ARCHIVE_MESSAGES = 20


@dataclass(frozen=True)
class SessionArchivePayload:
    path: str
    rel_path: str
    content: str
    source_session_id: str
    source_message_count: int
    included_message_count: int


def build_session_archive_payload(
    *,
    worktree: str,
    source_session: Session,
    trigger_session: Session,
    history: list[MessageWithParts],
    now: datetime,
) -> SessionArchivePayload | None:
    conversation_blocks = _conversation_blocks(history)
    if not conversation_blocks:
        return None

    total_messages = len(conversation_blocks)
    included_blocks = conversation_blocks[-_MAX_ARCHIVE_MESSAGES:]
    timestamp = _format_timestamp(now)
    archive_path = resolve_daily_archive_path(worktree=worktree, now=now)
    rel_path = archive_path.relative_to(Path(worktree).resolve()).as_posix()

    lines = [
        _archive_marker(source_session.id),
        f"## Session Archive: {timestamp}",
        "",
        f"- Source session: {source_session.title}",
        f"- Source session ID: `{source_session.id}`",
        f"- Triggered by new session: {trigger_session.title}",
        f"- Trigger session ID: `{trigger_session.id}`",
        f"- Included messages: {len(included_blocks)}/{total_messages}",
        "",
        "### Conversation",
        "",
        "\n\n".join(included_blocks),
    ]
    return SessionArchivePayload(
        path=str(archive_path),
        rel_path=rel_path,
        content="\n".join(lines).strip() + "\n",
        source_session_id=source_session.id,
        source_message_count=total_messages,
        included_message_count=len(included_blocks),
    )


def resolve_daily_archive_path(*, worktree: str, now: datetime) -> Path:
    worktree_path = Path(worktree).resolve()
    return worktree_path / "memory" / f"{now.strftime('%Y-%m-%d')}.md"


def append_archive_entry(existing: str, entry: str) -> str:
    head = existing.rstrip()
    if not head:
        return entry
    return f"{head}\n\n{entry}"


def archive_entry_exists(existing: str, session_id: str) -> bool:
    return _archive_marker(session_id) in existing


def _archive_marker(session_id: str) -> str:
    return f"{_ARCHIVE_MARKER_PREFIX} {session_id} -->"


def _conversation_blocks(history: list[MessageWithParts]) -> list[str]:
    blocks: list[str] = []
    for message in history:
        role = message.info.role
        if role not in {"user", "assistant"}:
            continue
        parts: list[str] = []
        for part in message.parts:
            if getattr(part, "type", None) != "text":
                continue
            text = " ".join(str(getattr(part, "text", "")).split())
            if text:
                parts.append(text)
        if not parts:
            continue
        blocks.append(f"**{role.title()}**\n\n" + "\n\n".join(parts))
    return blocks


def _format_timestamp(now: datetime) -> str:
    tz_name = now.tzname() or now.strftime("%z") or "local"
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {tz_name}"
