from __future__ import annotations

from typing import Callable, Protocol

from pydantic import BaseModel


# ── Data models ──


class VLMJobStatus(BaseModel):
    job_id: str
    state: str  # pending | running | done | failed
    extracted_pages: int | None = None
    total_pages: int | None = None
    error_msg: str | None = None


class VLMParseResult(BaseModel):
    markdown_pages: list[str]  # one markdown string per page


# ── Protocol ──


class VLMParser(Protocol):
    async def submit(self, file_bytes: bytes, filename: str) -> str:
        """Submit file for parsing, return job_id."""
        ...

    async def get_status(self, job_id: str) -> VLMJobStatus:
        """Check job status and progress."""
        ...

    async def get_result(self, job_id: str) -> VLMParseResult:
        """Fetch completed result (call only when status is done)."""
        ...

    async def wait_for_result(
        self,
        job_id: str,
        poll_interval: int = 5,
        timeout: int = 1800,
        on_progress: Callable[[VLMJobStatus], None] | None = None,
    ) -> VLMParseResult:
        """Poll until done/failed, invoking on_progress(status) on each poll."""
        ...
