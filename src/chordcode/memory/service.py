from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from chordcode.config import Config
from chordcode.log import logger
from chordcode.memory.embeddings import EmbeddingProvider, build_embedding_provider
from chordcode.memory.manager import MemoryManager, build_memory_db_path
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
            return
        await self.ensure_worktree(self._cfg.default_worktree)
        for session in await self._store.list_sessions(limit=10_000, offset=0):
            if session.runtime.backend == "local":
                await self.ensure_worktree(session.worktree)
        if self._task is None:
            self._task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

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
                    await manager.sync()
                except Exception as exc:
                    _log.warning(
                        "Memory sync failed",
                        event="memory.sync.error",
                        worktree=worktree,
                        error=str(exc),
                    )

    @staticmethod
    def _default_provider_factory(cfg: Config) -> EmbeddingProvider | None:
        return build_embedding_provider(
            base_url=cfg.memory.embedding_base_url,
            api_key=cfg.memory.embedding_api_key,
            model=cfg.memory.embedding_model,
        )

