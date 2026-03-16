from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import aiosqlite

from chordcode.memory.types import MemoryChunk, MemoryFileRecord, MemoryHit


class MemoryIndexStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @property
    def path(self) -> str:
        return self._db_path

    async def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                  path TEXT PRIMARY KEY,
                  hash TEXT NOT NULL,
                  mtime_ms INTEGER NOT NULL,
                  size INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                  id TEXT PRIMARY KEY,
                  path TEXT NOT NULL,
                  source TEXT NOT NULL,
                  start_line INTEGER NOT NULL,
                  end_line INTEGER NOT NULL,
                  hash TEXT NOT NULL,
                  text TEXT NOT NULL,
                  embedding_json TEXT
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(
                  text,
                  id UNINDEXED,
                  path UNINDEXED,
                  source UNINDEXED,
                  start_line UNINDEXED,
                  end_line UNINDEXED
                )
                """
            )
            await db.commit()

    async def list_files(self) -> dict[str, MemoryFileRecord]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT path, hash, mtime_ms, size FROM files")
            rows = await cur.fetchall()
        return {
            str(row[0]): MemoryFileRecord(
                path=str(row[0]),
                hash=str(row[1]),
                mtime_ms=int(row[2]),
                size=int(row[3]),
            )
            for row in rows
        }

    async def get_embeddings_for_path(self, path: str) -> dict[str, list[float]]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT hash, embedding_json FROM chunks WHERE path=? AND embedding_json IS NOT NULL",
                (path,),
            )
            rows = await cur.fetchall()
        out: dict[str, list[float]] = {}
        for chunk_hash, embedding_json in rows:
            try:
                parsed = json.loads(str(embedding_json))
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                out[str(chunk_hash)] = [float(value) for value in parsed]
        return out

    async def replace_file(
        self,
        *,
        record: MemoryFileRecord,
        chunks: list[MemoryChunk],
        embeddings: dict[str, list[float]],
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM chunks WHERE path=?", (record.path,))
            await db.execute("DELETE FROM chunks_fts WHERE path=?", (record.path,))
            await db.execute(
                """
                INSERT INTO files(path, hash, mtime_ms, size)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  hash=excluded.hash,
                  mtime_ms=excluded.mtime_ms,
                  size=excluded.size
                """,
                (record.path, record.hash, record.mtime_ms, record.size),
            )
            for chunk in chunks:
                embedding = embeddings.get(chunk.hash)
                await db.execute(
                    """
                    INSERT INTO chunks(
                      id, path, source, start_line, end_line, hash, text, embedding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.path,
                        chunk.source,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.hash,
                        chunk.text,
                        json.dumps(embedding) if embedding is not None else None,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO chunks_fts(text, id, path, source, start_line, end_line)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.text,
                        chunk.id,
                        chunk.path,
                        chunk.source,
                        chunk.start_line,
                        chunk.end_line,
                    ),
                )
            await db.commit()

    async def delete_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        async with aiosqlite.connect(self._db_path) as db:
            for path in paths:
                await db.execute("DELETE FROM files WHERE path=?", (path,))
                await db.execute("DELETE FROM chunks WHERE path=?", (path,))
                await db.execute("DELETE FROM chunks_fts WHERE path=?", (path,))
            await db.commit()

    async def update_meta(self, key: str, value: dict[str, Any]) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, json.dumps(value)),
            )
            await db.commit()

    async def read_meta(self, key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT value FROM meta WHERE key=?", (key,))
            row = await cur.fetchone()
        if not row:
            return None
        try:
            parsed = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def search_bm25(self, *, query: str, limit: int) -> list[tuple[str, MemoryHit]]:
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                SELECT id, path, start_line, end_line, source, text, bm25(chunks_fts) AS rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, limit),
            )
            rows = await cur.fetchall()
        hits: list[tuple[str, MemoryHit]] = []
        for row in rows:
            rank = float(row[6]) if row[6] is not None else 999.0
            score = 1.0 / (1.0 + max(rank, 0.0))
            hits.append(
                (
                    str(row[0]),
                    MemoryHit(
                        path=str(row[1]),
                        start_line=int(row[2]),
                        end_line=int(row[3]),
                        score=score,
                        snippet=_truncate(str(row[5])),
                        source=str(row[4]),
                    ),
                )
            )
        return hits

    async def load_vector_rows(self) -> list[tuple[str, MemoryHit, list[float] | None]]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                SELECT id, path, start_line, end_line, source, text, embedding_json
                FROM chunks
                """
            )
            rows = await cur.fetchall()
        out: list[tuple[str, MemoryHit, list[float] | None]] = []
        for row in rows:
            parsed: list[float] | None = None
            if row[6]:
                try:
                    raw = json.loads(str(row[6]))
                except json.JSONDecodeError:
                    raw = None
                if isinstance(raw, list):
                    parsed = [float(value) for value in raw]
            out.append(
                (
                    str(row[0]),
                    MemoryHit(
                        path=str(row[1]),
                        start_line=int(row[2]),
                        end_line=int(row[3]),
                        score=0.0,
                        snippet=_truncate(str(row[5])),
                        source=str(row[4]),
                    ),
                    parsed,
                )
            )
        return out

    async def read_file(self, path: str) -> str | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT path FROM files WHERE path=?", (path,))
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def stats(self) -> dict[str, int]:
        async with aiosqlite.connect(self._db_path) as db:
            cur_files = await db.execute("SELECT COUNT(*) FROM files")
            file_row = await cur_files.fetchone()
            cur_archives = await db.execute("SELECT COUNT(*) FROM files WHERE path LIKE 'memory/%'")
            archive_row = await cur_archives.fetchone()
            cur_chunks = await db.execute("SELECT COUNT(*) FROM chunks")
            chunk_row = await cur_chunks.fetchone()
        return {
            "indexed_file_count": int(file_row[0] or 0),
            "indexed_archive_file_count": int(archive_row[0] or 0),
            "indexed_chunk_count": int(chunk_row[0] or 0),
        }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _build_fts_query(raw: str) -> str | None:
    tokens = [token.strip() for token in raw.replace("/", " ").replace("-", " ").split() if token.strip()]
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def _truncate(text: str, limit: int = 700) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
