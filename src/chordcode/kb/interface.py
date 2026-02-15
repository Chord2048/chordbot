from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


# ── Data models ──


class KBDocument(BaseModel):
    id: str
    content_summary: str
    content_length: int
    status: str  # PENDING | PROCESSING | PROCESSED | FAILED
    created_at: str | None = None
    updated_at: str | None = None
    file_path: str | None = None
    chunks_count: int | None = None
    error_msg: str | None = None


class KBDocumentPage(BaseModel):
    documents: list[KBDocument]
    page: int
    page_size: int
    total_count: int
    total_pages: int
    warning: str | None = None


class KBInsertResult(BaseModel):
    status: str  # success | duplicated | failure
    message: str
    track_id: str


class KBStatusCounts(BaseModel):
    counts: dict[str, int]  # {PENDING: 2, PROCESSED: 10, ...}


class KBPipelineStatus(BaseModel):
    busy: bool
    job_name: str | None = None
    docs_total: int | None = None
    docs_current: int | None = None


class KBEntity(BaseModel):
    entity_name: str
    entity_type: str | None = None
    description: str | None = None
    source_id: str | None = None
    file_path: str | None = None


class KBRelationship(BaseModel):
    src_id: str
    tgt_id: str
    description: str | None = None
    keywords: str | None = None
    weight: float | None = None


class KBChunk(BaseModel):
    content: str
    file_path: str | None = None
    chunk_id: str | None = None


class KBQueryResult(BaseModel):
    entities: list[KBEntity]
    relationships: list[KBRelationship]
    chunks: list[KBChunk]


# ── Protocol ──


class KBBackend(Protocol):
    async def upload_file(self, file_bytes: bytes, filename: str) -> KBInsertResult: ...

    async def delete_documents(self, doc_ids: list[str]) -> dict[str, Any]: ...

    async def list_documents(
        self, page: int = 1, page_size: int = 20, status_filter: str | None = None,
    ) -> KBDocumentPage: ...

    async def get_pipeline_status(self) -> KBPipelineStatus: ...

    async def get_status_counts(self) -> KBStatusCounts: ...

    async def query(self, query: str, top_k: int = 10) -> KBQueryResult: ...
