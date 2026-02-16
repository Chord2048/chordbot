from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from chordcode.tools.base import ToolResult
from chordcode.tools.truncate import truncate


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCRIPT_STYLE_RE = re.compile(r"<(?P<tag>script|style)[^>]*>[\s\S]*?</(?P=tag)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", re.IGNORECASE)
_HEADING_RE = re.compile(r"<h([1-6])[^>]*>([\s\S]*?)</h\1>", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"<li[^>]*>([\s\S]*?)</li>", re.IGNORECASE)

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
_DEFAULT_TIMEOUT_MS = 30_000
_MAX_TIMEOUT_MS = 120_000
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class WebSearchCtx:
    tavily_api_key: str
    timeout_ms: int = _DEFAULT_TIMEOUT_MS


@dataclass(frozen=True)
class WebFetchCtx:
    timeout_ms: int = _DEFAULT_TIMEOUT_MS
    max_response_bytes: int = _MAX_RESPONSE_BYTES


class TavilySearchTool:
    name = "websearch"
    description = "Search the web using Tavily with a focused set of high-signal parameters."

    def __init__(self, ctx: WebSearchCtx | None = None) -> None:
        if ctx is None:
            raise RuntimeError("WebSearchCtx is required for TavilySearchTool")
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "topic": {"type": "string", "enum": ["general", "news", "finance"], "default": "general"},
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year", "d", "w", "m", "y"],
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "include_answer": {
                    "oneOf": [
                        {"type": "boolean"},
                        {"type": "string", "enum": ["basic", "advanced"]},
                    ],
                },
                "include_domains": {"type": "array", "items": {"type": "string"}},
                "exclude_domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        api_key = self._ctx.tavily_api_key.strip()
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY is required for websearch")

        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")

        timeout_ms = self._ctx.timeout_ms

        payload: dict[str, Any] = {"query": query}

        max_results = args.get("max_results")
        if max_results is not None:
            max_results = int(max_results)
            if max_results < 1 or max_results > 20:
                raise ValueError("max_results must be in [1, 20]")
            payload["max_results"] = max_results

        topic = args.get("topic")
        if topic is not None:
            topic = str(topic)
            if topic not in {"general", "news", "finance"}:
                raise ValueError("topic must be one of general, news, finance")
            payload["topic"] = topic

        time_range = args.get("time_range")
        if time_range is not None:
            time_range = str(time_range)
            if time_range not in {"day", "week", "month", "year", "d", "w", "m", "y"}:
                raise ValueError("time_range must be one of day/week/month/year/d/w/m/y")
            payload["time_range"] = time_range

        start_date = args.get("start_date")
        if start_date is not None:
            start_date = str(start_date).strip()
            if not _DATE_RE.fullmatch(start_date):
                raise ValueError("start_date must be YYYY-MM-DD")
            payload["start_date"] = start_date

        end_date = args.get("end_date")
        if end_date is not None:
            end_date = str(end_date).strip()
            if not _DATE_RE.fullmatch(end_date):
                raise ValueError("end_date must be YYYY-MM-DD")
            payload["end_date"] = end_date

        include_answer = args.get("include_answer")
        if include_answer is not None:
            if not isinstance(include_answer, bool) and include_answer not in {"basic", "advanced"}:
                raise ValueError("include_answer must be a boolean or one of basic/advanced")
            payload["include_answer"] = include_answer

        include_domains = args.get("include_domains")
        if include_domains is not None:
            if not isinstance(include_domains, list) or not all(isinstance(v, str) for v in include_domains):
                raise ValueError("include_domains must be an array of strings")
            if len(include_domains) > 300:
                raise ValueError("include_domains supports at most 300 domains")
            payload["include_domains"] = include_domains

        exclude_domains = args.get("exclude_domains")
        if exclude_domains is not None:
            if not isinstance(exclude_domains, list) or not all(isinstance(v, str) for v in exclude_domains):
                raise ValueError("exclude_domains must be an array of strings")
            if len(exclude_domains) > 150:
                raise ValueError("exclude_domains supports at most 150 domains")
            payload["exclude_domains"] = exclude_domains

        await ctx.ask(
            permission="websearch",
            patterns=[query],
            always=["*"],
            metadata={
                "query": query,
                "topic": payload.get("topic"),
                "max_results": payload.get("max_results"),
            },
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
                resp = await client.post(_TAVILY_SEARCH_URL, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise TimeoutError(f"Tavily request timed out after {timeout_ms}ms") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Tavily request failed: {e}") from e

        if not resp.is_success:
            detail = _extract_http_error(resp)
            raise RuntimeError(f"Tavily request failed ({resp.status_code}): {detail}")

        data = resp.json()
        output_json = json.dumps(data, ensure_ascii=False, indent=2)
        t = truncate(output_json)

        results = data.get("results") if isinstance(data, dict) else None
        result_count = len(results) if isinstance(results, list) else 0

        return ToolResult(
            title=f"Web search: {query}",
            output=t.content,
            metadata={
                "query": query,
                "result_count": result_count,
                "truncated": t.truncated,
                "request_id": data.get("request_id") if isinstance(data, dict) else None,
                "response_time": data.get("response_time") if isinstance(data, dict) else None,
            },
        )


class WebFetchTool:
    name = "webfetch"
    description = "Fetch URL content via async HTTP and return extracted markdown/text/html output."

    def __init__(self, ctx: WebFetchCtx | None = None) -> None:
        self._ctx = ctx or WebFetchCtx()

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch"},
                "format": {
                    "type": "string",
                    "enum": ["markdown", "text", "html"],
                    "default": "markdown",
                    "description": "Output format",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": _MAX_TIMEOUT_MS,
                    "description": "Request timeout in milliseconds",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 500000,
                    "description": "Max characters in returned output",
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            raise ValueError("url is required")
        if not _is_http_url(url):
            raise ValueError("url must start with http:// or https:// and include a host")

        output_format = str(args.get("format") or "markdown").strip().lower()
        if output_format not in {"markdown", "text", "html"}:
            raise ValueError("format must be one of markdown, text, html")

        timeout_ms = _clamp_timeout_ms(args.get("timeout_ms"), default_ms=self._ctx.timeout_ms)
        max_chars = int(args.get("max_chars") or 120_000)
        if max_chars < 500 or max_chars > 500_000:
            raise ValueError("max_chars must be in [500, 500000]")

        await ctx.ask(
            permission="webfetch",
            patterns=[url],
            always=["*"],
            metadata={"url": url, "format": output_format, "timeout_ms": timeout_ms},
        )

        headers = {
            "User-Agent": _DEFAULT_USER_AGENT,
            "Accept": _accept_header_for_format(output_format),
            "Accept-Language": "en-US,en;q=0.9",
        }

        try:
            async with httpx.AsyncClient(
                timeout=timeout_ms / 1000,
                follow_redirects=True,
                max_redirects=5,
            ) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            raise TimeoutError(f"webfetch timed out after {timeout_ms}ms") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"webfetch request failed: {e}") from e

        if not resp.is_success:
            raise RuntimeError(f"webfetch request failed with status {resp.status_code}")

        content_length_header = resp.headers.get("content-length")
        if content_length_header is not None:
            try:
                content_length = int(content_length_header)
                if content_length > self._ctx.max_response_bytes:
                    raise RuntimeError(
                        f"webfetch response too large ({content_length} bytes > {self._ctx.max_response_bytes} bytes)"
                    )
            except ValueError:
                pass

        body_bytes = resp.content
        if len(body_bytes) > self._ctx.max_response_bytes:
            raise RuntimeError(
                f"webfetch response too large ({len(body_bytes)} bytes > {self._ctx.max_response_bytes} bytes)"
            )

        content_type = (resp.headers.get("content-type") or "").lower()
        raw_text = resp.text

        if output_format == "html":
            rendered = raw_text
        else:
            if _looks_like_html(content_type, raw_text):
                rendered = _html_to_markdown(raw_text) if output_format == "markdown" else _html_to_text(raw_text)
            else:
                rendered = raw_text

        t = truncate(rendered, max_chars=max_chars)
        final_url = str(resp.url)

        return ToolResult(
            title=f"Web fetch: {url}",
            output=t.content,
            metadata={
                "url": url,
                "final_url": final_url,
                "status_code": resp.status_code,
                "content_type": content_type,
                "format": output_format,
                "truncated": t.truncated,
                "response_bytes": len(body_bytes),
            },
        )


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clamp_timeout_ms(value: Any, *, default_ms: int) -> int:
    if value is None:
        return default_ms
    timeout_ms = int(value)
    if timeout_ms < 1000:
        return 1000
    if timeout_ms > _MAX_TIMEOUT_MS:
        return _MAX_TIMEOUT_MS
    return timeout_ms


def _extract_http_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            err = detail.get("error")
            if err:
                return str(err)
        if detail:
            return str(detail)
        message = payload.get("message")
        if message:
            return str(message)

    text = (resp.text or "").strip()
    return text or "request failed"


def _accept_header_for_format(output_format: str) -> str:
    if output_format == "markdown":
        return "text/markdown;q=1.0, text/plain;q=0.9, text/html;q=0.8, */*;q=0.1"
    if output_format == "text":
        return "text/plain;q=1.0, text/html;q=0.9, */*;q=0.1"
    return "text/html;q=1.0, application/xhtml+xml;q=0.9, */*;q=0.1"


def _looks_like_html(content_type: str, text: str) -> bool:
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        return True
    prefix = text[:256].lstrip().lower()
    return prefix.startswith("<!doctype html") or prefix.startswith("<html")


def _html_to_text(html_text: str) -> str:
    text = _SCRIPT_STYLE_RE.sub("", html_text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|section|article|h[1-6]|tr|table)>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG_RE.sub("", text)
    return _normalize(unescape(text))


def _html_to_markdown(html_text: str) -> str:
    text = _SCRIPT_STYLE_RE.sub("", html_text)

    text = _ANCHOR_RE.sub(lambda m: f"[{_html_to_text(m.group(2))}]({m.group(1)})", text)

    text = _HEADING_RE.sub(
        lambda m: f"\n{'#' * int(m.group(1))} {_html_to_text(m.group(2))}\n",
        text,
    )

    text = _LIST_ITEM_RE.sub(lambda m: f"\n- {_html_to_text(m.group(1))}", text)

    text = re.sub(r"(?i)</(p|div|section|article|tr|table)>", "\n\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)

    text = _TAG_RE.sub("", text)
    return _normalize(unescape(text))


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
