from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from chordcode.config import MemoryConfig
from chordcode.log import logger
from chordcode.memory.chunking import chunk_markdown, hash_text
from chordcode.memory.embeddings import EmbeddingProvider
from chordcode.memory.store import MemoryIndexStore, cosine_similarity
from chordcode.memory.types import MemoryFileRecord, MemoryHit

_log = logger.child(service="memory")


class MemoryManager:
    def __init__(
        self,
        *,
        worktree: str,
        config: MemoryConfig,
        db_path: str,
        embedding_provider: EmbeddingProvider | None,
    ) -> None:
        self.worktree = str(Path(worktree).resolve())
        self._config = config
        self._db = MemoryIndexStore(db_path)
        self._embedding_provider = embedding_provider
        self._lock = asyncio.Lock()
        self._schedule_lock = asyncio.Lock()
        self._last_sync_ms = 0
        self._sync_task: asyncio.Task[None] | None = None

    @property
    def db_path(self) -> str:
        return self._db.path

    async def init(self) -> None:
        await self._db.init()
        meta = await self._db.read_meta("index_state")
        if meta and isinstance(meta.get("last_sync_ms"), int):
            self._last_sync_ms = int(meta["last_sync_ms"])
        _log.info(
            "Memory manager ready",
            event="memory.manager.ready",
            worktree=self.worktree,
            db_path=self._db.path,
        )

    async def sync_if_stale(self) -> None:
        if not await self._is_index_stale():
            return
        await self.sync()

    async def schedule_sync_if_stale(self) -> bool:
        return await self.schedule_sync(force=False)

    async def schedule_sync(self, *, force: bool = False) -> bool:
        async with self._schedule_lock:
            current = self._sync_task
            if current is not None and not current.done():
                return False
            if not force and not await self._is_index_stale():
                return False
            self._sync_task = asyncio.create_task(self._run_scheduled_sync())
            return True

    async def wait_for_sync(self) -> None:
        task = self._sync_task
        current = asyncio.current_task()
        if task is None or task.done() or task is current:
            return
        await task

    async def close(self) -> None:
        task = self._sync_task
        current = asyncio.current_task()
        if task is None or task.done() or task is current:
            self._sync_task = None
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def sync(self) -> None:
        task = self._sync_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            await task
            return
        async with self._lock:
            files = await self._scan_files()
            indexed = await self._db.list_files()
            current_paths = set(files.keys())
            indexed_paths = set(indexed.keys())
            added = sorted(current_paths - indexed_paths)
            removed = sorted(indexed_paths - current_paths)
            changed = sorted(
                path
                for path, record in files.items()
                if path in indexed
                and (
                    indexed[path].hash != record.hash
                    or indexed[path].size != record.size
                    or indexed[path].mtime_ms != record.mtime_ms
                )
            )

            if added or changed or removed:
                _log.info(
                    "Memory file changes detected",
                    event="memory.sync.change_detected",
                    worktree=self.worktree,
                    added_paths=added,
                    changed_paths=changed,
                    removed_paths=removed,
                    source_file_count=len(files),
                )

            if removed:
                await self._db.delete_paths(removed)

            for path, record in files.items():
                existing = indexed.get(path)
                if existing and existing.hash == record.hash and existing.size == record.size:
                    continue
                await self._replace_file(record)

            self._last_sync_ms = int(time.time() * 1000)
            await self._db.update_meta(
                "index_state",
                {
                    "last_sync_ms": self._last_sync_ms,
                    "worktree": self.worktree,
                    "db_path": self._db.path,
                },
            )
            stats = await self.describe_sources()
            _log.info(
                "Memory sync completed",
                event="memory.sync.completed",
                worktree=self.worktree,
                source_file_count=stats["source_file_count"],
                archive_file_count=stats["archive_file_count"],
                indexed_file_count=stats["indexed_file_count"],
                indexed_chunk_count=stats["indexed_chunk_count"],
                watched_paths=stats["watched_paths"],
            )

    async def search(
        self,
        *,
        query: str,
        max_results: int = 5,
        min_score: float = 0.15,
    ) -> dict[str, object]:
        await self.schedule_sync_if_stale()
        query_text = query.strip()
        if not query_text:
            return {"hits": [], "stats": self._stats(search_mode="empty")}

        keyword_hits = await self._db.search_bm25(query=query_text, limit=max(max_results * 8, 20))
        keyword_weight = 0.35 if self._embedding_provider is not None else 1.0
        merged: dict[str, MemoryHit] = {
            chunk_id: MemoryHit(
                path=hit.path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                source=hit.source,
                snippet=hit.snippet,
                score=hit.score * keyword_weight,
            )
            for chunk_id, hit in keyword_hits
        }
        search_mode = "bm25"
        warning: str | None = None

        if self._embedding_provider is not None:
            try:
                vector_hits = await self._search_vector(query_text)
                search_mode = "hybrid"
                for chunk_id, vector_hit in vector_hits:
                    existing = merged.get(chunk_id)
                    if existing is None:
                        merged[chunk_id] = MemoryHit(
                            path=vector_hit.path,
                            start_line=vector_hit.start_line,
                            end_line=vector_hit.end_line,
                            source=vector_hit.source,
                            snippet=vector_hit.snippet,
                            score=vector_hit.score * 0.65,
                        )
                        continue
                    merged[chunk_id] = MemoryHit(
                        path=existing.path,
                        start_line=existing.start_line,
                        end_line=existing.end_line,
                        source=existing.source,
                        snippet=existing.snippet if len(existing.snippet) >= len(vector_hit.snippet) else vector_hit.snippet,
                        score=existing.score + (vector_hit.score * 0.65),
                    )
            except Exception as exc:
                warning = f"vector search unavailable: {exc}"
                _log.warning("Memory vector search failed", event="memory.search.vector_error", error=str(exc))
                merged = {chunk_id: hit for chunk_id, hit in keyword_hits}

        hits = sorted((hit for hit in merged.values() if hit.score >= min_score), key=lambda item: item.score, reverse=True)
        payload: dict[str, object] = {
            "hits": [
                {
                    "path": hit.path,
                    "start_line": hit.start_line,
                    "end_line": hit.end_line,
                    "score": round(hit.score, 4),
                    "snippet": hit.snippet,
                    "source": hit.source,
                }
                for hit in hits[:max_results]
            ],
            "stats": self._stats(search_mode=search_mode),
        }
        if warning:
            payload["warning"] = warning
        _log.debug(
            "Memory search completed",
            event="memory.search.completed",
            worktree=self.worktree,
            query=query_text,
            hit_count=len(payload["hits"]),
            search_mode=payload["stats"]["search_mode"],
        )
        return payload

    async def read_file(
        self,
        *,
        path: str,
        from_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, object]:
        rel_path = _normalize_memory_path(path)
        if rel_path is None:
            raise ValueError("path must be memory.md or under memory/")

        abs_path = Path(self.worktree, rel_path)
        if not abs_path.exists():
            return {
                "path": rel_path,
                "from_line": max(1, from_line),
                "to_line": max(0, from_line - 1),
                "text": "",
            }

        content = abs_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        start = max(1, int(from_line))
        count = max(1, int(max_lines))
        sliced = lines[start - 1 : start - 1 + count]
        end = start + len(sliced) - 1 if sliced else start - 1
        return {
            "path": rel_path,
            "from_line": start,
            "to_line": end,
            "text": "\n".join(sliced),
        }

    async def describe_sources(self) -> dict[str, object]:
        files = await self._scan_files()
        archive_paths = sorted(path for path in files if path.startswith("memory/"))
        db_stats = await self._db.stats()
        return {
            "memory_dir": str(Path(self.worktree, "memory")),
            "has_memory_md": "memory.md" in files,
            "source_file_count": len(files),
            "archive_file_count": len(archive_paths),
            "archive_paths": archive_paths,
            "watched_paths": sorted(files.keys()),
            **db_stats,
        }

    def index_age_ms(self) -> int | None:
        if self._last_sync_ms <= 0:
            return None
        return max(0, int(time.time() * 1000) - self._last_sync_ms)

    def _stats(self, *, search_mode: str) -> dict[str, object]:
        return {
            "worktree": self.worktree,
            "index_age_ms": self.index_age_ms(),
            "search_mode": search_mode,
        }

    async def _replace_file(self, record: MemoryFileRecord) -> None:
        abs_path = Path(self.worktree, record.path)
        content = abs_path.read_text(encoding="utf-8")
        chunks = chunk_markdown(path=record.path, content=content)
        existing_embeddings = await self._db.get_embeddings_for_path(record.path)
        missing_hashes = list(dict.fromkeys(chunk.hash for chunk in chunks if chunk.hash not in existing_embeddings))

        if missing_hashes and self._embedding_provider is not None:
            texts = []
            seen_hashes: set[str] = set()
            for chunk in chunks:
                if chunk.hash in missing_hashes and chunk.hash not in seen_hashes:
                    texts.append(chunk.text)
                    seen_hashes.add(chunk.hash)
            embeddings = await self._embedding_provider.embed(texts)
            embedding_iter = iter(embeddings)
            for chunk_hash in missing_hashes:
                if chunk_hash in existing_embeddings:
                    continue
                existing_embeddings[chunk_hash] = next(embedding_iter)

        await self._db.replace_file(record=record, chunks=chunks, embeddings=existing_embeddings)

    async def _search_vector(self, query: str) -> list[tuple[str, MemoryHit]]:
        assert self._embedding_provider is not None
        [query_embedding] = await self._embedding_provider.embed([query])
        rows = await self._db.load_vector_rows()
        scored: list[tuple[str, MemoryHit]] = []
        for chunk_id, base_hit, embedding in rows:
            if embedding is None:
                continue
            score = cosine_similarity(query_embedding, embedding)
            scored.append(
                (
                    chunk_id,
                    MemoryHit(
                        path=base_hit.path,
                        start_line=base_hit.start_line,
                        end_line=base_hit.end_line,
                        source=base_hit.source,
                        snippet=base_hit.snippet,
                        score=score,
                    ),
                )
            )
        scored.sort(key=lambda item: item[1].score, reverse=True)
        return scored

    async def _scan_files(self) -> dict[str, MemoryFileRecord]:
        worktree = Path(self.worktree)
        candidates = self._memory_candidates(worktree)

        out: dict[str, MemoryFileRecord] = {}
        for path in candidates:
            stat = path.stat()
            content = path.read_text(encoding="utf-8")
            rel_path = path.relative_to(worktree).as_posix()
            out[rel_path] = MemoryFileRecord(
                path=rel_path,
                hash=hash_text(content),
                mtime_ms=int(stat.st_mtime * 1000),
                size=stat.st_size,
            )
        return out

    async def _run_scheduled_sync(self) -> None:
        try:
            await self.sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning(
                "Scheduled memory sync failed",
                event="memory.sync.scheduled_error",
                worktree=self.worktree,
                error=str(exc),
            )
        finally:
            async with self._schedule_lock:
                current = asyncio.current_task()
                if self._sync_task is current:
                    self._sync_task = None

    async def _is_index_stale(self) -> bool:
        now = int(time.time() * 1000)
        if self._last_sync_ms <= 0:
            _log.debug("Memory index stale: never synced", event="memory.sync.stale_detected", worktree=self.worktree, reason="never_synced")
            return True
        if now - self._last_sync_ms >= max(int(self._config.sync_interval_seconds), 1) * 1000:
            _log.debug(
                "Memory index stale: sync interval exceeded",
                event="memory.sync.stale_detected",
                worktree=self.worktree,
                reason="interval",
            )
            return True

        indexed = await self._db.list_files()
        worktree = Path(self.worktree)
        candidates = self._memory_candidates(worktree)
        candidate_paths = {path.relative_to(worktree).as_posix(): path for path in candidates}
        if set(indexed.keys()) != set(candidate_paths.keys()):
            _log.debug(
                "Memory index stale: file set changed",
                event="memory.sync.stale_detected",
                worktree=self.worktree,
                reason="file_set_changed",
            )
            return True
        for rel_path, path in candidate_paths.items():
            stat = path.stat()
            indexed_record = indexed.get(rel_path)
            if indexed_record is None:
                _log.debug(
                    "Memory index stale: missing indexed record",
                    event="memory.sync.stale_detected",
                    worktree=self.worktree,
                    reason="missing_indexed_record",
                    path=rel_path,
                )
                return True
            if indexed_record.size != stat.st_size or indexed_record.mtime_ms != int(stat.st_mtime * 1000):
                _log.debug(
                    "Memory index stale: file changed",
                    event="memory.sync.stale_detected",
                    worktree=self.worktree,
                    reason="file_changed",
                    path=rel_path,
                )
                return True
        return False

    @staticmethod
    def _memory_candidates(worktree: Path) -> list[Path]:
        candidates: list[Path] = []
        root_file = worktree / "memory.md"
        if root_file.is_file():
            candidates.append(root_file)
        archive_dir = worktree / "memory"
        if archive_dir.is_dir():
            candidates.extend(sorted(path for path in archive_dir.rglob("*.md") if path.is_file()))
        return candidates


def build_memory_db_path(base_db_path: str, worktree: str) -> str:
    resolved_db = Path(base_db_path).expanduser().resolve()
    worktree_hash = hashlib.sha256(str(Path(worktree).resolve()).encode("utf-8")).hexdigest()
    return str(resolved_db.parent / "memory" / f"{worktree_hash}.sqlite3")


def _normalize_memory_path(path: str) -> str | None:
    candidate = path.strip().replace("\\", "/").lstrip("./")
    if candidate == "memory.md":
        return candidate
    if candidate.startswith("memory/") and candidate.endswith(".md"):
        return candidate
    return None
