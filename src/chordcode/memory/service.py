from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Callable

from chordcode.config import Config
from chordcode.log import logger
from chordcode.memory.archive import append_archive_entry, archive_entry_exists, build_session_archive_payload
from chordcode.memory.embeddings import EmbeddingProvider, build_embedding_provider
from chordcode.memory.manager import MemoryManager, build_memory_db_path
from chordcode.model import Session
from chordcode.store.sqlite import SQLiteStore

_log = logger.child(service="memory.service")


class MemoryService:
    def __init__(
        self,
        *,
        cfg: Config,
        store: SQLiteStore,
        embedding_provider_factory: Callable[[Config], EmbeddingProvider | None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._provider_factory = embedding_provider_factory or self._default_provider_factory
        self._managers: dict[str, MemoryManager] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._cfg.memory.enabled

    async def start(self) -> None:
        if not self.enabled:
            _log.info("Memory service disabled", event="memory.service.disabled")
            return
        _log.info(
            "Memory service starting",
            event="memory.service.starting",
            default_worktree=self._cfg.default_worktree,
            sync_interval_seconds=self._cfg.memory.sync_interval_seconds,
        )
        await self.ensure_worktree(self._cfg.default_worktree)
        for session in await self._store.list_sessions(limit=10_000, offset=0):
            if session.runtime.backend == "local":
                await self.ensure_worktree(session.worktree)
        if self._task is None:
            self._task = asyncio.create_task(self._sync_loop())
            _log.info("Memory background sync loop started", event="memory.service.loop_started")

    async def stop(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for manager in list(self._managers.values()):
            await manager.close()
        _log.info("Memory service stopped", event="memory.service.stopped")

    async def ensure_worktree(self, worktree: str) -> MemoryManager | None:
        if not self.enabled:
            return None
        resolved = str(Path(worktree).resolve())
        async with self._lock:
            manager = self._managers.get(resolved)
            if manager is not None:
                return manager
            provider = self._provider_factory(self._cfg)
            manager = MemoryManager(
                worktree=resolved,
                config=self._cfg.memory,
                db_path=build_memory_db_path(self._cfg.db_path, resolved),
                embedding_provider=provider,
            )
            await manager.init()
            self._managers[resolved] = manager
            stats = await manager.describe_sources()
            _log.info(
                "Memory monitoring enabled for worktree",
                event="memory.watch.enabled",
                worktree=resolved,
                memory_dir=stats["memory_dir"],
                has_memory_md=stats["has_memory_md"],
                source_file_count=stats["source_file_count"],
                archive_file_count=stats["archive_file_count"],
                indexed_file_count=stats["indexed_file_count"],
                indexed_chunk_count=stats["indexed_chunk_count"],
                watched_paths=stats["watched_paths"],
                db_path=manager.db_path,
            )
            return manager

    async def get_manager(self, worktree: str) -> MemoryManager | None:
        resolved = str(Path(worktree).resolve())
        manager = self._managers.get(resolved)
        if manager is not None:
            return manager
        return await self.ensure_worktree(resolved)

    async def _sync_loop(self) -> None:
        interval = max(int(self._cfg.memory.sync_interval_seconds), 1)
        while not self._closed:
            await asyncio.sleep(interval)
            for worktree, manager in list(self._managers.items()):
                try:
                    await manager.schedule_sync_if_stale()
                except Exception as exc:
                    _log.warning(
                        "Memory sync failed",
                        event="memory.sync.error",
                        worktree=worktree,
                        error=str(exc),
                    )

    async def archive_previous_session_for_new_session(self, new_session: Session) -> str | None:
        if not self.enabled or new_session.runtime.backend != "local":
            return None

        previous = await self._find_previous_local_session(new_session)
        if previous is None:
            _log.debug(
                "No previous local session to archive",
                event="memory.archive.skipped",
                worktree=new_session.worktree,
                trigger_session_id=new_session.id,
                reason="no_previous_session",
            )
            return None

        history = await self._store.list_messages(previous.id)
        now = datetime.now().astimezone()
        payload = build_session_archive_payload(
            worktree=new_session.worktree,
            source_session=previous,
            trigger_session=new_session,
            history=history,
            now=now,
        )
        if payload is None:
            _log.debug(
                "Previous session has no archiveable conversation",
                event="memory.archive.skipped",
                worktree=new_session.worktree,
                source_session_id=previous.id,
                trigger_session_id=new_session.id,
                reason="empty_history",
            )
            return None

        archive_path = Path(payload.path)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        if archive_entry_exists(existing, previous.id):
            _log.debug(
                "Session archive entry already exists",
                event="memory.archive.skipped",
                worktree=new_session.worktree,
                source_session_id=previous.id,
                trigger_session_id=new_session.id,
                archive_path=payload.rel_path,
                reason="already_archived",
            )
            return payload.rel_path

        archive_path.write_text(append_archive_entry(existing, payload.content), encoding="utf-8")
        manager = await self.ensure_worktree(new_session.worktree)
        if manager is not None:
            await manager.schedule_sync(force=True)

        _log.info(
            "Archived previous session into memory log",
            event="memory.archive.session",
            worktree=new_session.worktree,
            source_session_id=previous.id,
            source_session_title=previous.title,
            trigger_session_id=new_session.id,
            trigger_session_title=new_session.title,
            archive_path=payload.rel_path,
            source_message_count=payload.source_message_count,
            included_message_count=payload.included_message_count,
            sync_scheduled=manager is not None,
        )
        return payload.rel_path

    async def _find_previous_local_session(self, new_session: Session) -> Session | None:
        resolved_worktree = str(Path(new_session.worktree).resolve())
        sessions = await self._store.list_sessions(limit=10_000, offset=0)
        candidates = [
            session
            for session in sessions
            if session.id != new_session.id
            and session.kind == "primary"
            and session.runtime.backend == "local"
            and str(Path(session.worktree).resolve()) == resolved_worktree
            and session.created_at <= new_session.created_at
        ]
        candidates.sort(key=lambda session: (session.updated_at, session.created_at), reverse=True)
        return candidates[0] if candidates else None

    @staticmethod
    def _default_provider_factory(cfg: Config) -> EmbeddingProvider | None:
        return build_embedding_provider(
            base_url=cfg.memory.embedding_base_url,
            api_key=cfg.memory.embedding_api_key,
            model=cfg.memory.embedding_model,
        )
