from __future__ import annotations

import posixpath
import re
import shlex
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any, Protocol, cast

from chordcode.log import logger
from chordcode.runtime import DaytonaManager
from chordcode.tools.base import ToolResult
from chordcode.tools.truncate import truncate

_MAX_RESULTS = 100
_MAX_LINE_LENGTH = 2000
_LOG_PREVIEW_MAX = 200

_log = logger.child(service="tool.daytona")


@dataclass(frozen=True)
class DaytonaCtx:
    worktree: str
    cwd: str
    sandbox: Any
    sandbox_id: str
    manager: DaytonaManager


class DaytonaProcessLike(Protocol):
    def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> Any: ...


class DaytonaExecResultLike(Protocol):
    exit_code: int
    result: str | None
    stderr: str | None


class DaytonaFsLike(Protocol):
    def download_file(self, path: str) -> bytes | str | None: ...
    def upload_file(self, src: bytes, dst: str, timeout: int = 1800) -> None: ...
    def create_folder(self, path: str, mode: str) -> None: ...
    def list_files(self, path: str) -> Any: ...
    def search_files(self, path: str, pattern: str) -> Any: ...
    def find_files(self, path: str, pattern: str) -> list[Any]: ...


def _resolve_remote_path(*, cwd: str, file_path: str) -> str:
    if not file_path:
        return posixpath.normpath(cwd or "/")
    if file_path.startswith("/"):
        return posixpath.normpath(file_path)
    return posixpath.normpath(posixpath.join(cwd or "/", file_path))


def _is_within_remote(*, root: str, path: str) -> bool:
    root_norm = posixpath.normpath(root or "/")
    path_norm = posixpath.normpath(path or "/")
    if not root_norm.startswith("/"):
        root_norm = "/" + root_norm
    if not path_norm.startswith("/"):
        path_norm = "/" + path_norm
    try:
        return posixpath.commonpath([root_norm, path_norm]) == root_norm
    except Exception:
        return False


def _dict_or_attr(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _list_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    files = _dict_or_attr(value, "files")
    if isinstance(files, list):
        return files
    return []


def _entry_is_dir(entry: Any) -> bool:
    value = _dict_or_attr(entry, "is_dir")
    if isinstance(value, bool):
        return value
    # SDK fields can vary across versions (`isDir` or `type`).
    value = _dict_or_attr(entry, "isDir")
    if isinstance(value, bool):
        return value
    typ = _dict_or_attr(entry, "type")
    return isinstance(typ, str) and typ.lower() in {"dir", "directory", "folder"}


def _entry_path(base: str, entry: Any) -> str:
    raw_path = _dict_or_attr(entry, "path")
    if isinstance(raw_path, str) and raw_path:
        return posixpath.normpath(raw_path)
    raw_name = _dict_or_attr(entry, "name")
    if isinstance(raw_name, str) and raw_name:
        return posixpath.normpath(posixpath.join(base, raw_name))
    return posixpath.normpath(base)


def _extract_exec(result: Any) -> tuple[int, str, str]:
    if result is None:
        return 0, "", ""
    if isinstance(result, str):
        return 0, result, ""
    if isinstance(result, tuple) and len(result) >= 2:
        code = int(result[0]) if isinstance(result[0], int) else 0
        out = str(result[1] or "")
        err = str(result[2] or "") if len(result) >= 3 else ""
        return code, out, err

    if isinstance(result, dict):
        code = result.get("exit_code", 0)
        out = result.get("result", "")
        err = result.get("stderr", "")
        try:
            code_int = int(code or 0)
        except Exception:
            code_int = 0
        return code_int, str(out or ""), str(err or "")

    typed = cast(DaytonaExecResultLike, result)
    try:
        code = int(getattr(typed, "exit_code", 0) or 0)
    except Exception:
        code = 0
    out = str(getattr(typed, "result", "") or "")
    err = str(getattr(typed, "stderr", "") or "")
    return code, out, err


def _process_exec(sandbox: Any, command: str, *, cwd: str, timeout_ms: int | None = None) -> tuple[int, str, str]:
    try:
        process = cast(DaytonaProcessLike, sandbox.process)
        result = process.exec(command=command, cwd=cwd, timeout=timeout_ms)
        return _extract_exec(result)
    except Exception as exc:
        raise RuntimeError(f"daytona process execution failed: {exc}") from exc


def _fs_download(fs: Any, path: str) -> str:
    try:
        typed_fs = cast(DaytonaFsLike, fs)
        result = typed_fs.download_file(path)
    except Exception as exc:
        raise RuntimeError(f"daytona fs.download_file failed: {exc}") from exc
    if result is None:
        return ""
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode("utf-8", errors="replace")
    return str(result)


def _fs_upload(fs: Any, path: str, content: str) -> None:
    data = content.encode("utf-8")
    try:
        typed_fs = cast(DaytonaFsLike, fs)
        typed_fs.upload_file(data, path)
    except Exception as exc:
        raise RuntimeError(f"daytona fs.upload_file failed: {exc}") from exc


def _fs_mkdir(fs: Any, path: str) -> None:
    typed_fs = cast(DaytonaFsLike, fs)
    norm = posixpath.normpath(path or "")
    if not norm or norm == "/":
        return
    parts = [p for p in norm.split("/") if p]
    cursor = ""
    for part in parts:
        cursor = f"{cursor}/{part}" if cursor else f"/{part}"
        try:
            typed_fs.create_folder(cursor, "755")
        except Exception:
            # Directory may already exist; continue recursively.
            continue


def _fs_list(fs: Any, path: str) -> list[Any]:
    try:
        typed_fs = cast(DaytonaFsLike, fs)
        result = typed_fs.list_files(path)
        return _list_items(result)
    except Exception:
        return []


def _fs_search_files(fs: Any, path: str, pattern: str) -> list[str]:
    try:
        typed_fs = cast(DaytonaFsLike, fs)
        result = typed_fs.search_files(path, pattern)
        return [str(item).strip() for item in _list_items(result) if str(item).strip()]
    except Exception as exc:
        raise RuntimeError(f"daytona fs.search_files failed: {exc}") from exc


def _fs_find_files(fs: Any, path: str, pattern: str) -> list[tuple[str, int, str]]:
    try:
        typed_fs = cast(DaytonaFsLike, fs)
        result_value = typed_fs.find_files(path, pattern)
    except Exception as exc:
        raise RuntimeError(f"daytona fs.find_files failed: {exc}") from exc

    out: list[tuple[str, int, str]] = []
    for item in _list_items(result_value):
        file_path = _dict_or_attr(item, "file")
        line = _dict_or_attr(item, "line")
        content = _dict_or_attr(item, "content")
        if not isinstance(file_path, str) or not file_path.strip():
            continue
        try:
            line_no = int(line) if line is not None else 0
        except Exception:
            line_no = 0
        out.append((file_path.strip(), max(1, line_no), str(content or "")))
    return out


def _walk_files(fs: Any, root: str) -> list[str]:
    out: list[str] = []
    queue = [posixpath.normpath(root)]
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for entry in _fs_list(fs, current):
            p = _entry_path(current, entry)
            if _entry_is_dir(entry):
                queue.append(p)
            else:
                out.append(p)
    return out


def _match_glob(pattern: str, root: str, file_path: str) -> bool:
    rel = str(PurePosixPath(file_path).relative_to(PurePosixPath(root))) if file_path.startswith(root) else file_path
    rel = rel.lstrip("/")
    if fnmatch(rel, pattern):
        return True
    if fnmatch(posixpath.basename(rel), pattern):
        return True
    if pattern.startswith("./") and fnmatch(rel, pattern[2:]):
        return True
    return False


def _preview(text: str, limit: int = _LOG_PREVIEW_MAX) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _check_rg(ctx: DaytonaCtx, tool_ctx) -> bool:
    cached = ctx.manager.get_cached_rg_available(ctx.sandbox_id)
    if cached is not None:
        _log.debug(
            "reuse cached ripgrep availability",
            event="daytona.rg.cache_hit",
            session_id=tool_ctx.session_id,
            sandbox_id=ctx.sandbox_id,
            available=cached,
        )
        return cached

    code, out, _ = _process_exec(ctx.sandbox, "command -v rg", cwd=ctx.cwd, timeout_ms=10_000)
    if code == 0 and out.strip():
        _log.info(
            "ripgrep detected in Daytona sandbox",
            event="daytona.rg.available",
            session_id=tool_ctx.session_id,
            sandbox_id=ctx.sandbox_id,
            rg_path=out.strip(),
        )
        ctx.manager.set_cached_rg_available(ctx.sandbox_id, True)
        return True

    logger.warning(
        "ripgrep not found in Daytona sandbox",
        event="daytona.rg.missing",
        session_id=tool_ctx.session_id,
        sandbox_id=ctx.sandbox_id,
    )
    if not ctx.manager.rg_install_attempted(ctx.sandbox_id):
        ctx.manager.mark_rg_install_attempted(ctx.sandbox_id)
        install_commands = [
            "apt-get update && apt-get install -y ripgrep",
            "apk add --no-cache ripgrep",
            "dnf install -y ripgrep",
            "yum install -y ripgrep",
            "microdnf install -y ripgrep",
            "pacman -Sy --noconfirm ripgrep",
        ]
        _log.info(
            "attempting to install ripgrep in Daytona sandbox",
            event="daytona.rg.install_attempt",
            session_id=tool_ctx.session_id,
            sandbox_id=ctx.sandbox_id,
            attempts=len(install_commands),
        )
        for install_cmd in install_commands:
            code, out_text, err_text = _process_exec(ctx.sandbox, install_cmd, cwd=ctx.cwd, timeout_ms=120_000)
            _log.info(
                "ripgrep install command finished",
                event="daytona.rg.install_command",
                session_id=tool_ctx.session_id,
                sandbox_id=ctx.sandbox_id,
                command=install_cmd,
                returncode=code,
                stdout_preview=_preview(out_text),
                stderr_preview=_preview(err_text),
            )
            if code == 0:
                break

    code, out, _ = _process_exec(ctx.sandbox, "command -v rg", cwd=ctx.cwd, timeout_ms=10_000)
    if code == 0 and out.strip():
        _log.info(
            "ripgrep install succeeded",
            event="daytona.rg.install_succeeded",
            session_id=tool_ctx.session_id,
            sandbox_id=ctx.sandbox_id,
            rg_path=out.strip(),
        )
        ctx.manager.set_cached_rg_available(ctx.sandbox_id, True)
        return True

    logger.warning(
        "failed to install ripgrep in Daytona sandbox",
        event="daytona.rg.install_failed",
        session_id=tool_ctx.session_id,
        sandbox_id=ctx.sandbox_id,
    )
    ctx.manager.set_cached_rg_available(ctx.sandbox_id, False)
    return False


class DaytonaBashTool:
    name = "bash"
    description = "Execute a shell command within the Daytona sandbox session context."

    def __init__(self, ctx: DaytonaCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["command"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command is required")

        workdir = _resolve_remote_path(cwd=self._ctx.cwd, file_path=str(args.get("workdir") or self._ctx.cwd))
        timeout_ms = int(args.get("timeout_ms") or 120_000)
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be >= 0")

        if not _is_within_remote(root=self._ctx.worktree, path=workdir):
            await ctx.ask(
                permission="external_directory",
                patterns=[workdir],
                always=[posixpath.join(posixpath.dirname(workdir), "*")],
                metadata={},
            )

        words = shlex.split(command, posix=True) if command else []
        cmd_name = words[0] if words else command.split(" ", 1)[0]
        always = [f"{cmd_name}*"] if cmd_name else ["*"]
        await ctx.ask(permission="bash", patterns=[command], always=always, metadata={"workdir": workdir})

        t0 = time.monotonic()
        _log.info(
            "daytona bash started",
            event="tool.daytona.bash.start",
            session_id=ctx.session_id,
            sandbox_id=self._ctx.sandbox_id,
            workdir=workdir,
            timeout_ms=timeout_ms,
            command_preview=_preview(command),
        )
        try:
            code, out, err = _process_exec(self._ctx.sandbox, command, cwd=workdir, timeout_ms=timeout_ms)
            output = out or ""
            if err:
                output = f"{output}\n{err}".strip()
            t = truncate(output)
            await ctx.tool_stream_update(t.content)
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.info(
                "daytona bash completed",
                event="tool.daytona.bash.done",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                returncode=code,
                output_len=len(t.content),
                truncated=t.truncated,
            )
            return ToolResult(
                title="bash",
                output=t.content,
                metadata={"returncode": code, "truncated": t.truncated, "workdir": workdir},
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.error(
                "daytona bash failed",
                event="tool.daytona.bash.error",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                workdir=workdir,
                command_preview=_preview(command),
                error=str(exc),
                exc_info=exc,
            )
            raise


class DaytonaReadTool:
    name = "read"
    description = "Read a text file from the Daytona sandbox worktree."

    def __init__(self, ctx: DaytonaCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["file_path"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        file_path = str(args.get("file_path", "")).strip()
        if not file_path:
            raise ValueError("file_path is required")
        path = _resolve_remote_path(cwd=self._ctx.cwd, file_path=file_path)

        if not _is_within_remote(root=self._ctx.worktree, path=path):
            await ctx.ask(
                permission="external_directory",
                patterns=[path],
                always=[posixpath.join(posixpath.dirname(path), "*")],
                metadata={},
            )
        await ctx.ask(permission="read", patterns=[path], always=["*"], metadata={})

        t0 = time.monotonic()
        offset = int(args.get("offset") or 0)
        limit = int(args.get("limit") or 2000)
        _log.info(
            "daytona read started",
            event="tool.daytona.read.start",
            session_id=ctx.session_id,
            sandbox_id=self._ctx.sandbox_id,
            path=path,
            offset=offset,
            limit=limit,
        )
        try:
            content = _fs_download(self._ctx.sandbox.fs, path)
            lines = content.splitlines()
            chunk = lines[offset : offset + limit]
            text = "\n".join(chunk)
            t = truncate(text)
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.info(
                "daytona read completed",
                event="tool.daytona.read.done",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                path=path,
                total_lines=len(lines),
                output_len=len(t.content),
                truncated=t.truncated,
            )
            return ToolResult(
                title=path,
                output=t.content,
                metadata={"truncated": t.truncated, "offset": offset, "limit": limit, "total_lines": len(lines)},
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.error(
                "daytona read failed",
                event="tool.daytona.read.error",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                path=path,
                error=str(exc),
                exc_info=exc,
            )
            raise


class DaytonaWriteTool:
    name = "write"
    description = "Write a text file under the Daytona sandbox worktree."

    def __init__(self, ctx: DaytonaCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        file_path = str(args.get("file_path", "")).strip()
        content = str(args.get("content", ""))
        if not file_path:
            raise ValueError("file_path is required")
        path = _resolve_remote_path(cwd=self._ctx.cwd, file_path=file_path)

        if not _is_within_remote(root=self._ctx.worktree, path=path):
            await ctx.ask(
                permission="external_directory",
                patterns=[path],
                always=[posixpath.join(posixpath.dirname(path), "*")],
                metadata={},
            )
        await ctx.ask(permission="write", patterns=[path], always=["*"], metadata={})
        t0 = time.monotonic()
        _log.info(
            "daytona write started",
            event="tool.daytona.write.start",
            session_id=ctx.session_id,
            sandbox_id=self._ctx.sandbox_id,
            path=path,
            content_len=len(content),
        )
        try:
            parent = posixpath.dirname(path)
            if parent and parent != "/":
                _fs_mkdir(self._ctx.sandbox.fs, parent)
            _fs_upload(self._ctx.sandbox.fs, path, content)
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.info(
                "daytona write completed",
                event="tool.daytona.write.done",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                path=path,
                content_len=len(content),
            )
            return ToolResult(title=path, output="Wrote file successfully.", metadata={"file_path": path})
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.error(
                "daytona write failed",
                event="tool.daytona.write.error",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                path=path,
                content_len=len(content),
                error=str(exc),
                exc_info=exc,
            )
            raise


class DaytonaGlobTool:
    name = "glob"
    description = "Find files by glob pattern under a directory in the Daytona sandbox."

    def __init__(self, ctx: DaytonaCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match files"},
                "path": {"type": "string", "description": "Directory to search in (defaults to current working directory)"},
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern is required")
        search_root = _resolve_remote_path(cwd=self._ctx.cwd, file_path=str(args.get("path") or self._ctx.cwd))

        if not _is_within_remote(root=self._ctx.worktree, path=search_root):
            await ctx.ask(
                permission="external_directory",
                patterns=[search_root],
                always=[posixpath.join(posixpath.dirname(search_root), "*")],
                metadata={"tool": "glob"},
            )
        await ctx.ask(
            permission="glob",
            patterns=[pattern],
            always=["*"],
            metadata={"pattern": pattern, "path": args.get("path")},
        )

        t0 = time.monotonic()
        _log.info(
            "daytona glob started",
            event="tool.daytona.glob.start",
            session_id=ctx.session_id,
            sandbox_id=self._ctx.sandbox_id,
            search_root=search_root,
            pattern=pattern,
        )
        try:
            files: list[str] = []
            fallback_mode = "search_files"
            try:
                files = _fs_search_files(self._ctx.sandbox.fs, search_root, pattern)
            except Exception as exc:
                fallback_mode = "emulation"
                logger.warning(
                    "daytona search_files unavailable for glob; falling back to emulation",
                    event="daytona.glob.search_files_fallback",
                    session_id=ctx.session_id,
                    sandbox_id=self._ctx.sandbox_id,
                    error=str(exc),
                )
                all_files = _walk_files(self._ctx.sandbox.fs, search_root)
                files = [p for p in all_files if _match_glob(pattern, search_root, p)]

            normalized: list[str] = []
            for p in files:
                raw = str(p).strip()
                if not raw:
                    continue
                if raw.startswith("/"):
                    normalized.append(posixpath.normpath(raw))
                else:
                    normalized.append(_resolve_remote_path(cwd=search_root, file_path=raw))

            files = sorted(dict.fromkeys(normalized))
            truncated = len(files) > _MAX_RESULTS
            final_files = files[:_MAX_RESULTS]
            output_lines: list[str] = final_files or ["No files found"]
            if truncated:
                output_lines += ["", "(Results are truncated. Consider using a more specific path or pattern.)"]
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.info(
                "daytona glob completed",
                event="tool.daytona.glob.done",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                search_root=search_root,
                pattern=pattern,
                mode=fallback_mode,
                total_matches=len(files),
                returned=len(final_files),
                truncated=truncated,
            )
            return ToolResult(
                title=search_root,
                output="\n".join(output_lines),
                metadata={"count": len(final_files), "truncated": truncated},
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.error(
                "daytona glob failed",
                event="tool.daytona.glob.error",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                search_root=search_root,
                pattern=pattern,
                error=str(exc),
                exc_info=exc,
            )
            raise


class DaytonaGrepTool:
    name = "grep"
    description = "Search file contents with a regex pattern in the Daytona sandbox."

    def __init__(self, ctx: DaytonaCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search in (defaults to current working directory)"},
                "include": {"type": "string", "description": 'File include glob (e.g. "*.py", "*.{ts,tsx}")'},
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern is required")
        include_value = args.get("include")
        include = str(include_value).strip() if include_value is not None else None
        if include == "":
            include = None

        search_root = _resolve_remote_path(cwd=self._ctx.cwd, file_path=str(args.get("path") or self._ctx.cwd))
        if not _is_within_remote(root=self._ctx.worktree, path=search_root):
            await ctx.ask(
                permission="external_directory",
                patterns=[search_root],
                always=[posixpath.join(posixpath.dirname(search_root), "*")],
                metadata={"tool": "grep"},
            )
        await ctx.ask(
            permission="grep",
            patterns=[pattern],
            always=["*"],
            metadata={"pattern": pattern, "path": args.get("path"), "include": include},
        )

        t0 = time.monotonic()
        _log.info(
            "daytona grep started",
            event="tool.daytona.grep.start",
            session_id=ctx.session_id,
            sandbox_id=self._ctx.sandbox_id,
            search_root=search_root,
            pattern=pattern,
            include=include,
        )
        try:
            matches: list[tuple[str, int, str]] = []
            mode = "find_files"
            try:
                matches = _fs_find_files(self._ctx.sandbox.fs, search_root, pattern)
            except Exception as exc:
                mode = "fallback"
                logger.warning(
                    "daytona find_files unavailable for grep; falling back to rg/emulation",
                    event="daytona.grep.find_files_fallback",
                    session_id=ctx.session_id,
                    sandbox_id=self._ctx.sandbox_id,
                    error=str(exc),
                )
                if _check_rg(self._ctx, ctx):
                    mode = "rg"
                    cmd = f"rg -n --hidden --no-messages {shlex.quote(pattern)} {shlex.quote(search_root)}"
                    if include:
                        cmd = f"{cmd} --glob {shlex.quote(include)}"
                    code, out, _ = _process_exec(self._ctx.sandbox, cmd, cwd=self._ctx.cwd, timeout_ms=120_000)
                    if code in (0, 1):
                        for line in out.splitlines():
                            parts = line.split(":", 2)
                            if len(parts) != 3:
                                continue
                            fp, ln, text = parts
                            try:
                                line_no = int(ln)
                            except Exception:
                                continue
                            matches.append((fp, line_no, text))
                else:
                    mode = "emulation"
                    regex = re.compile(pattern)
                    files = _walk_files(self._ctx.sandbox.fs, search_root)
                    for fp in files:
                        rel = str(PurePosixPath(fp).relative_to(PurePosixPath(search_root))) if fp.startswith(search_root) else fp
                        rel = rel.lstrip("/")
                        if include and not (fnmatch(rel, include) or fnmatch(posixpath.basename(rel), include)):
                            continue
                        try:
                            content = _fs_download(self._ctx.sandbox.fs, fp)
                        except Exception:
                            continue
                        for idx, line in enumerate(content.splitlines(), start=1):
                            if regex.search(line):
                                matches.append((fp, idx, line))

            if include:
                filtered: list[tuple[str, int, str]] = []
                for fp, line_no, text in matches:
                    rel = str(PurePosixPath(fp).relative_to(PurePosixPath(search_root))) if fp.startswith(search_root) else fp
                    rel = rel.lstrip("/")
                    if fnmatch(rel, include) or fnmatch(posixpath.basename(rel), include):
                        filtered.append((fp, line_no, text))
                matches = filtered

            if not matches:
                duration_ms = int((time.monotonic() - t0) * 1000)
                _log.info(
                    "daytona grep completed with no matches",
                    event="tool.daytona.grep.done",
                    session_id=ctx.session_id,
                    sandbox_id=self._ctx.sandbox_id,
                    duration_ms=duration_ms,
                    search_root=search_root,
                    pattern=pattern,
                    include=include,
                    mode=mode,
                    matches=0,
                    truncated=False,
                )
                return ToolResult(title=pattern, output="No files found", metadata={"matches": 0, "truncated": False})

            truncated = len(matches) > _MAX_RESULTS
            final = matches[:_MAX_RESULTS]
            output_lines: list[str] = [f"Found {len(final)} matches"]
            current_file = ""
            for fp, line_no, text in final:
                if current_file != fp:
                    if current_file:
                        output_lines.append("")
                    current_file = fp
                    output_lines.append(f"{fp}:")
                line_text = text if len(text) <= _MAX_LINE_LENGTH else text[:_MAX_LINE_LENGTH] + "..."
                output_lines.append(f"  Line {line_no}: {line_text}")
            if truncated:
                output_lines += ["", "(Results are truncated. Consider using a more specific path or pattern.)"]

            t = truncate("\n".join(output_lines))
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.info(
                "daytona grep completed",
                event="tool.daytona.grep.done",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                search_root=search_root,
                pattern=pattern,
                include=include,
                mode=mode,
                matches=len(final),
                truncated=truncated or t.truncated,
            )
            return ToolResult(
                title=pattern,
                output=t.content,
                metadata={"matches": len(final), "truncated": truncated or t.truncated},
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log.error(
                "daytona grep failed",
                event="tool.daytona.grep.error",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
                duration_ms=duration_ms,
                search_root=search_root,
                pattern=pattern,
                include=include,
                error=str(exc),
                exc_info=exc,
            )
            raise
