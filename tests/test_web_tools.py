from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.tools.web import TavilySearchTool, WebFetchTool, WebSearchCtx


@dataclass
class FakeToolCtx:
    session_id: str = "s1"
    message_id: str = "m1"
    agent: str = "primary"
    asks: list[dict] = field(default_factory=list)

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict) -> None:
        self.asks.append(
            {
                "permission": permission,
                "patterns": patterns,
                "always": always,
                "metadata": metadata,
            }
        )

    async def tool_stream_update(self, output: str) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, *, response: httpx.Response, recorder: dict[str, object]) -> None:
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, *, headers: dict[str, str] | None = None, json: dict | None = None):
        self._recorder["method"] = "POST"
        self._recorder["url"] = url
        self._recorder["headers"] = headers or {}
        self._recorder["json"] = json or {}
        return self._response

    async def get(self, url: str, *, headers: dict[str, str] | None = None):
        self._recorder["method"] = "GET"
        self._recorder["url"] = url
        self._recorder["headers"] = headers or {}
        return self._response


class WebToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_websearch_requests_permission_and_calls_tavily(self) -> None:
        recorder: dict[str, object] = {}
        payload = {
            "query": "python async",
            "answer": "Python supports async/await.",
            "results": [
                {
                    "title": "Async IO",
                    "url": "https://example.com/async",
                    "content": "A quick intro to async in Python",
                    "score": 0.9,
                }
            ],
            "images": [],
            "response_time": 0.7,
            "request_id": "rid-1",
        }
        response = httpx.Response(
            200,
            json=payload,
            request=httpx.Request("POST", "https://api.tavily.com/search"),
        )
        fake_client = _FakeAsyncClient(response=response, recorder=recorder)

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}, clear=False):
            tool = TavilySearchTool(WebSearchCtx(tavily_api_key="tvly-test"))

        ctx = FakeToolCtx()
        with patch("chordcode.tools.web.httpx.AsyncClient", return_value=fake_client):
            out = await tool.execute({"query": "python async", "max_results": 3}, ctx)

        self.assertEqual(ctx.asks[0]["permission"], "websearch")
        self.assertEqual(ctx.asks[0]["patterns"], ["python async"])
        self.assertEqual(recorder["method"], "POST")
        self.assertEqual(recorder["url"], "https://api.tavily.com/search")
        self.assertEqual((recorder["json"] or {}).get("query"), "python async")
        self.assertEqual((recorder["json"] or {}).get("max_results"), 3)
        self.assertTrue((recorder["headers"] or {}).get("Authorization", "").startswith("Bearer tvly-test"))
        self.assertIn("python async", out.output)
        self.assertEqual(out.metadata.get("result_count"), 1)

    async def test_websearch_requires_api_key(self) -> None:
        tool = TavilySearchTool(WebSearchCtx(tavily_api_key=""))

        ctx = FakeToolCtx()
        with self.assertRaises(RuntimeError):
            await tool.execute({"query": "test"}, ctx)

    async def test_webfetch_requests_permission_and_extracts_markdown(self) -> None:
        recorder: dict[str, object] = {}
        html = """
        <html><body>
          <h1>Hello World</h1>
          <p>This is a <a href=\"https://example.com/page\">sample page</a>.</p>
          <ul><li>Item A</li><li>Item B</li></ul>
        </body></html>
        """
        response = httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "https://example.com"),
        )
        fake_client = _FakeAsyncClient(response=response, recorder=recorder)

        tool = WebFetchTool()
        ctx = FakeToolCtx()
        with patch("chordcode.tools.web.httpx.AsyncClient", return_value=fake_client):
            out = await tool.execute({"url": "https://example.com", "format": "markdown"}, ctx)

        self.assertEqual(ctx.asks[0]["permission"], "webfetch")
        self.assertEqual(ctx.asks[0]["patterns"], ["https://example.com"])
        self.assertEqual(recorder["method"], "GET")
        self.assertEqual(recorder["url"], "https://example.com")
        self.assertIn("# Hello World", out.output)
        self.assertIn("[sample page](https://example.com/page)", out.output)
        self.assertEqual(out.metadata.get("status_code"), 200)
        self.assertEqual(out.metadata.get("format"), "markdown")

    async def test_webfetch_rejects_non_http_url(self) -> None:
        tool = WebFetchTool()
        ctx = FakeToolCtx()

        with self.assertRaises(ValueError):
            await tool.execute({"url": "ftp://example.com/file.txt"}, ctx)


if __name__ == "__main__":
    unittest.main()
