"""HTTP client wrapper for CLI → FastAPI communication."""
from __future__ import annotations

import sys
from typing import Any, AsyncIterator

import httpx


class APIError(Exception):
    """Raised when the server returns an error response."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class Client:
    """Thin async httpx wrapper targeting the Chord Code API."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _client(self, **kw: Any) -> httpx.AsyncClient:
        defaults = {"base_url": self._base, "timeout": self._timeout}
        defaults.update(kw)
        return httpx.AsyncClient(**defaults)

    async def _check(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = body.get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise APIError(resp.status_code, str(detail))
        return resp.json()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._client() as c:
            return await self._check(await c.get(path, params=params))

    async def post(self, path: str, json: Any = None) -> Any:
        async with self._client() as c:
            return await self._check(await c.post(path, json=json))

    async def put(self, path: str, json: Any = None) -> Any:
        async with self._client() as c:
            return await self._check(await c.put(path, json=json))

    async def patch(self, path: str, json: Any = None) -> Any:
        async with self._client() as c:
            return await self._check(await c.patch(path, json=json))

    async def delete(self, path: str, json: Any = None) -> Any:
        async with self._client() as c:
            return await self._check(await c.delete(path, json=json))

    async def upload_file(self, path: str, filepath: str, field: str = "file", params: dict[str, Any] | None = None) -> Any:
        import os
        filename = os.path.basename(filepath)
        async with self._client() as c:
            with open(filepath, "rb") as f:
                files = {field: (filename, f)}
                resp = await c.post(path, files=files, params=params)
            return await self._check(resp)

    async def stream_sse(self, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed SSE data dicts from the server event stream."""
        import json
        async with self._client(timeout=None) as c:
            async with c.stream("GET", path, params=params) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise APIError(resp.status_code, resp.text)
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            yield json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

    async def ping(self) -> bool:
        """Return True if the server is reachable."""
        try:
            async with self._client(timeout=5.0) as c:
                resp = await c.get("/config")
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, OSError):
            return False


def connect_or_exit(base_url: str, msg: str = "Cannot reach server") -> Client:
    """Return a Client instance. The caller should handle APIError."""
    return Client(base_url)
