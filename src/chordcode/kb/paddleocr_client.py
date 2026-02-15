from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

import httpx

from chordcode.kb.vlm_interface import VLMJobStatus, VLMParseResult
from chordcode.log import logger

_log = logger.child(service="kb.vlm")


class PaddleOCRClient:
    """VLMParser implementation backed by PaddleOCR async API."""

    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        poll_interval: int = 5,
        timeout: int = 1800,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._timeout = timeout
        _log.info(
            "PaddleOCR client created",
            event="vlm.init",
            api_url=self._api_url,
            auth=bool(api_key),
            poll_interval=poll_interval,
            timeout=timeout,
        )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._api_key:
            h["Authorization"] = f"bearer {self._api_key}"
        return h

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._api_url, headers=self._headers(), timeout=60)

    # ── submit ──

    async def submit(self, file_bytes: bytes, filename: str) -> str:
        _log.info("submitting file for VLM parsing", event="vlm.submit", filename=filename, size_bytes=len(file_bytes))
        t0 = time.monotonic()
        try:
            async with self._client() as c:
                r = await c.post(
                    "/api/v2/ocr/jobs",
                    files={"file": (filename, file_bytes)},
                    data={"model": "PaddleOCR-VL-1.5"},
                )
                r.raise_for_status()
                data = r.json()
            job_id = str(data["data"]["jobId"])
            _log.info(
                "VLM job submitted",
                event="vlm.submit.done",
                job_id=job_id,
                filename=filename,
                elapsed_s=round(time.monotonic() - t0, 2),
            )
            return job_id
        except Exception as exc:
            _log.error("VLM submit failed", event="vlm.submit.error", filename=filename, error=str(exc))
            raise

    # ── status ──

    async def get_status(self, job_id: str) -> VLMJobStatus:
        _log.debug("polling VLM job status", event="vlm.status", job_id=job_id)
        try:
            async with self._client() as c:
                r = await c.get(f"/api/v2/ocr/jobs/{job_id}")
                r.raise_for_status()
                data = r.json()

            job_data = data.get("data", {})
            # PaddleOCR uses "state" (not "status"): pending | running | done | failed
            state = str(job_data.get("state", "pending")).lower()

            progress = job_data.get("extractProgress", {})
            status = VLMJobStatus(
                job_id=job_id,
                state=state,
                extracted_pages=progress.get("extractedPages"),
                total_pages=progress.get("totalPages"),
                error_msg=job_data.get("errorMsg"),
            )
            _log.debug(
                "VLM job status polled",
                event="vlm.status.done",
                job_id=job_id,
                state=state,
                extracted_pages=status.extracted_pages,
                total_pages=status.total_pages,
            )
            return status
        except Exception as exc:
            _log.error("VLM status poll failed", event="vlm.status.error", job_id=job_id, error=str(exc))
            raise

    # ── result ──

    async def get_result(self, job_id: str) -> VLMParseResult:
        _log.info("fetching VLM result", event="vlm.result", job_id=job_id)
        t0 = time.monotonic()
        try:
            async with self._client() as c:
                r = await c.get(f"/api/v2/ocr/jobs/{job_id}")
                r.raise_for_status()
                data = r.json()

            json_url = data["data"]["resultUrl"]["jsonUrl"]
            _log.debug("downloading VLM result JSONL", event="vlm.result.download", job_id=job_id, json_url=json_url)

            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.get(json_url)
                r.raise_for_status()
                raw_text = r.text

            pages: list[str] = []
            for line in raw_text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    _log.debug("skipping non-JSON line in VLM result", event="vlm.result.skip_line", job_id=job_id)
                    continue
                results = obj.get("result", {}).get("layoutParsingResults", [])
                for item in results:
                    md_text = item.get("markdown", {}).get("text", "")
                    if md_text:
                        pages.append(md_text)

            elapsed = time.monotonic() - t0
            _log.info(
                "VLM result fetched",
                event="vlm.result.done",
                job_id=job_id,
                pages=len(pages),
                total_chars=sum(len(p) for p in pages),
                elapsed_s=round(elapsed, 2),
            )
            return VLMParseResult(markdown_pages=pages)
        except Exception as exc:
            _log.error("VLM result fetch failed", event="vlm.result.error", job_id=job_id, error=str(exc))
            raise

    # ── poll until done ──

    async def wait_for_result(
        self,
        job_id: str,
        poll_interval: int = 0,
        timeout: int = 0,
        on_progress: Callable[[VLMJobStatus], None] | None = None,
    ) -> VLMParseResult:
        interval = poll_interval or self._poll_interval
        max_wait = timeout or self._timeout
        elapsed = 0

        _log.info(
            "waiting for VLM result",
            event="vlm.wait",
            job_id=job_id,
            poll_interval=interval,
            timeout=max_wait,
        )
        t0 = time.monotonic()

        while elapsed < max_wait:
            status = await self.get_status(job_id)
            if on_progress:
                on_progress(status)
            if status.state == "done":
                result = await self.get_result(job_id)
                _log.info(
                    "VLM wait completed",
                    event="vlm.wait.done",
                    job_id=job_id,
                    pages=len(result.markdown_pages),
                    wall_s=round(time.monotonic() - t0, 2),
                )
                return result
            if status.state == "failed":
                _log.error("VLM job failed during wait", event="vlm.wait.failed", job_id=job_id, error=status.error_msg)
                raise RuntimeError(f"VLM job {job_id} failed: {status.error_msg}")
            await asyncio.sleep(interval)
            elapsed += interval

        _log.error("VLM job timed out", event="vlm.wait.timeout", job_id=job_id, timeout=max_wait)
        raise TimeoutError(f"VLM job {job_id} timed out after {max_wait}s")
