from __future__ import annotations

import time
from typing import Any

import httpx

from chordcode.kb.interface import (
    KBChunk,
    KBDocument,
    KBDocumentPage,
    KBEntity,
    KBInsertResult,
    KBPipelineStatus,
    KBQueryResult,
    KBRelationship,
    KBStatusCounts,
)
from chordcode.log import logger

_log = logger.child(service="kb.lightrag")


class LightRAGClient:
    """KBBackend implementation backed by a LightRAG Server."""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        _log.info("LightRAG client created", event="kb.init", base_url=self._base_url, auth=bool(api_key))

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers(), timeout=60)

    # ── insert ──

    async def upload_file(self, file_bytes: bytes, filename: str) -> KBInsertResult:
        """Upload a raw file to LightRAG via POST /documents/upload (multipart/form-data)."""
        _log.info("uploading file", event="kb.upload_file", filename=filename, size=len(file_bytes))
        t0 = time.monotonic()
        try:
            # Use a separate client without the default JSON Content-Type header;
            # httpx will set the correct multipart Content-Type automatically.
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            async with httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=120) as c:
                r = await c.post(
                    "/documents/upload",
                    files={"file": (filename, file_bytes)},
                )
                r.raise_for_status()
                data = r.json()
            result = KBInsertResult(
                status=data.get("status", "success"),
                message=data.get("message", ""),
                track_id=data.get("track_id", ""),
            )
            elapsed = time.monotonic() - t0
            _log.info(
                "file uploaded",
                event="kb.upload_file.done",
                filename=filename,
                status=result.status,
                track_id=result.track_id,
                elapsed_s=round(elapsed, 2),
            )
            return result
        except Exception as exc:
            _log.error("upload_file failed", event="kb.upload_file.error", filename=filename, error=str(exc), elapsed_s=round(time.monotonic() - t0, 2))
            raise

    # ── delete ──

    async def delete_documents(self, doc_ids: list[str]) -> dict[str, Any]:
        _log.info("deleting documents", event="kb.delete", count=len(doc_ids), doc_ids=doc_ids)
        try:
            async with self._client() as c:
                r = await c.request("DELETE", "/documents/delete_document", json={"ids": doc_ids})
                r.raise_for_status()
                data = r.json()
            _log.info("documents deleted", event="kb.delete.done", count=len(doc_ids))
            return data
        except Exception as exc:
            _log.error("delete_documents failed", event="kb.delete.error", error=str(exc), doc_ids=doc_ids)
            raise

    # ── list / status ──

    def _parse_doc_list(self, data: dict[str, Any], page: int, page_size: int) -> KBDocumentPage:
        """Parse a successful /documents/paginated response into KBDocumentPage."""
        docs = [
            KBDocument(
                id=d.get("id", ""),
                content_summary=d.get("content_summary", ""),
                content_length=d.get("content_length", 0),
                status=d.get("status", ""),
                created_at=d.get("created_at"),
                updated_at=d.get("updated_at"),
                file_path=d.get("file_path"),
                chunks_count=d.get("chunks_count"),
                error_msg=d.get("error_msg"),
            )
            for d in data.get("documents", [])
        ]
        pag = data.get("pagination", {})
        return KBDocumentPage(
            documents=docs,
            page=pag.get("page", page),
            page_size=pag.get("page_size", page_size),
            total_count=pag.get("total_count", 0),
            total_pages=pag.get("total_pages", 0),
        )

    async def _list_paginated(
        self, page: int, page_size: int, status_filter: str | None,
    ) -> dict[str, Any]:
        """Raw call to /documents/paginated. Raises on HTTP error."""
        payload: dict[str, Any] = {"page": page, "page_size": page_size}
        if status_filter:
            payload["status_filter"] = status_filter
        async with self._client() as c:
            r = await c.post("/documents/paginated", json=payload)
            r.raise_for_status()
            return r.json()

    async def _list_graceful_fallback(self, page: int, page_size: int) -> KBDocumentPage:
        """Fallback when /documents/paginated returns 500.

        LightRAG has a server-side bug where documents with file_path=None
        cause a Pydantic validation error. When this happens, return an empty
        doc list with total_count from status_counts so the UI can still
        show summary information.
        """
        _log.info("using graceful fallback for document listing", event="kb.list.fallback")
        total_count = 0
        try:
            counts_data = await self._get_status_counts_raw()
            total_count = counts_data.get("status_counts", {}).get("all", 0)
        except Exception:
            pass
        return KBDocumentPage(
            documents=[],
            page=page,
            page_size=page_size,
            total_count=total_count,
            total_pages=0,
            warning="LightRAG server error: some documents have missing metadata (file_path=None) causing the listing to fail. Documents still exist and can be queried. Try re-uploading with a file name to fix.",
        )

    async def _get_status_counts_raw(self) -> dict[str, Any]:
        async with self._client() as c:
            r = await c.get("/documents/status_counts")
            r.raise_for_status()
            return r.json()

    async def list_documents(
        self, page: int = 1, page_size: int = 20, status_filter: str | None = None,
    ) -> KBDocumentPage:
        _log.debug("listing documents", event="kb.list", page=page, page_size=page_size, status_filter=status_filter)
        try:
            data = await self._list_paginated(page, page_size, status_filter)
            result = self._parse_doc_list(data, page, page_size)
            _log.debug("documents listed", event="kb.list.done", count=len(result.documents), total=result.total_count)
            return result
        except httpx.HTTPStatusError as exc:
            # LightRAG server bug: documents with file_path=None cause 500.
            # Fall back to per-status queries which skip the broken documents.
            if exc.response.status_code == 500 and status_filter is None:
                _log.warning(
                    "list_documents got 500, using per-status fallback",
                    event="kb.list.fallback_trigger",
                    detail=exc.response.text[:500],
                )
                return await self._list_graceful_fallback(page, page_size)
            _log.error("list_documents failed", event="kb.list.error", error=str(exc))
            raise
        except Exception as exc:
            _log.error("list_documents failed", event="kb.list.error", error=str(exc))
            raise

    async def get_pipeline_status(self) -> KBPipelineStatus:
        _log.debug("fetching pipeline status", event="kb.pipeline_status")
        try:
            async with self._client() as c:
                r = await c.get("/documents/pipeline_status")
                r.raise_for_status()
                data = r.json()
            result = KBPipelineStatus(
                busy=data.get("busy", False),
                job_name=data.get("job_name"),
                docs_total=data.get("docs"),
                docs_current=data.get("cur_batch"),
            )
            _log.debug("pipeline status fetched", event="kb.pipeline_status.done", busy=result.busy)
            return result
        except Exception as exc:
            _log.error("get_pipeline_status failed", event="kb.pipeline_status.error", error=str(exc))
            raise

    async def get_status_counts(self) -> KBStatusCounts:
        _log.debug("fetching status counts", event="kb.status_counts")
        try:
            async with self._client() as c:
                r = await c.get("/documents/status_counts")
                r.raise_for_status()
                data = r.json()
            result = KBStatusCounts(counts=data.get("status_counts", data))
            _log.debug("status counts fetched", event="kb.status_counts.done", counts=result.counts)
            return result
        except Exception as exc:
            _log.error("get_status_counts failed", event="kb.status_counts.error", error=str(exc))
            raise

    # ── query (structured retrieval, mode=mix) ──

    async def query(self, query: str, top_k: int = 10) -> KBQueryResult:
        _log.info("querying KB", event="kb.query", query=query, top_k=top_k)
        t0 = time.monotonic()
        payload = {"query": query, "mode": "mix", "top_k": top_k}
        try:
            async with self._client() as c:
                r = await c.post("/query/data", json=payload)
                r.raise_for_status()
                raw = r.json()

            # LightRAG wraps results in {"status":..., "data": {entities, relationships, chunks}}
            data = raw.get("data", raw)

            entities = [
                KBEntity(
                    entity_name=e.get("entity_name", ""),
                    entity_type=e.get("entity_type"),
                    description=e.get("description"),
                    source_id=e.get("source_id"),
                    file_path=e.get("file_path"),
                )
                for e in data.get("entities", [])
            ]
            relationships = [
                KBRelationship(
                    src_id=r_.get("src_id", ""),
                    tgt_id=r_.get("tgt_id", ""),
                    description=r_.get("description"),
                    keywords=r_.get("keywords"),
                    weight=r_.get("weight"),
                )
                for r_ in data.get("relationships", [])
            ]
            chunks = [
                KBChunk(
                    content=ch.get("content", ""),
                    file_path=ch.get("file_path"),
                    chunk_id=ch.get("chunk_id"),
                )
                for ch in data.get("chunks", [])
            ]
            elapsed = time.monotonic() - t0
            _log.info(
                "KB query completed",
                event="kb.query.done",
                query=query,
                entities=len(entities),
                relationships=len(relationships),
                chunks=len(chunks),
                elapsed_s=round(elapsed, 2),
            )
            return KBQueryResult(entities=entities, relationships=relationships, chunks=chunks)
        except Exception as exc:
            _log.error(
                "KB query failed",
                event="kb.query.error",
                query=query,
                error=str(exc),
                elapsed_s=round(time.monotonic() - t0, 2),
            )
            raise
