from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from chordcode.log import logger
from chordcode.runtime import DaytonaManager
from chordcode.tools.base import ToolResult
from chordcode.tools.truncate import truncate

_MAX_RESULTS = 100
_MAX_LINE_LENGTH = 2000


@dataclass(frozen=True)
class DaytonaCtx:
    worktree: str
    cwd: str
    sandbox: Any
    sandbox_id: str
    manager: DaytonaManager


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


def _extract_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        for key in ("items", "files", "results", "data"):
            v = value.get(key)
            if isinstance(v, list):
                return v
    for key in ("items", "files", "results", "data"):
        v = getattr(value, key, None)
        if isinstance(v, list):
            return v
    return []


def _entry_get(entry: Any, *keys: str) -> Any:
    if isinstance(entry, dict):
        for key in keys:
            if key in entry:
                return entry[key]
        return None
    for key in keys:
        v = getattr(entry, key, None)
        if v is not None:
            return v
    return None


def _entry_is_dir(entry: Any) -> bool:
    value = _entry_get(entry, "is_dir", "isDir", "directory")
    if isinstance(value, bool):
        return value
    typ = _entry_get(entry, "type", "kind")
    if isinstance(typ, str):
        return typ.lower() in {"dir", "directory", "folder"}
    return False


def _entry_name(entry: Any) -> str | None:
    v = _entry_get(entry, "name", "filename", "file_name")
    if isinstance(v, str) and v:
        return v
    return None


def _entry_path(base: str, entry: Any) -> str:
    v = _entry_get(entry, "path", "full_path", "fullPath")
    if isinstance(v, str) and v:
        return posixpath.normpath(v)
    name = _entry_name(entry)
    if name:
        return posixpath.normpath(posixpath.join(base, name))
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

    code = _entry_get(result, "exit_code", "exitCode", "returncode", "code", "status")
    out = _entry_get(result, "result", "stdout", "output", "out")
    err = _entry_get(result, "stderr", "error", "err")
    try:
        code_int = int(code) if code is not None else 0
    except Exception:
        code_int = 0
    return code_int, str(out or ""), str(err or "")


def _process_exec(sandbox: Any, command: str, *, cwd: str, timeout_ms: int | None = None) -> tuple[int, str, str]:
    process = getattr(sandbox, "process", None)
    exec_fn = getattr(process, "exec", None)
    if not callable(exec_fn):
        raise RuntimeError("daytona sandbox process.exec is unavailable")

    errors: list[str] = []
    kwargs_variants: list[dict[str, Any]] = [
        {"command": command, "cwd": cwd, "timeout": timeout_ms},
        {"command": command, "cwd": cwd},
        {"command": command},
    ]
    for kwargs in kwargs_variants:
        try:
            result = exec_fn(**kwargs)
            return _extract_exec(result)
        except TypeError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:
            raise RuntimeError(f"daytona process execution failed: {exc}") from exc

    args_variants: list[tuple[Any, ...]] = [
        (command, cwd, timeout_ms),
        (command, cwd),
        (command,),
    ]
    for args in args_variants:
        try:
            result = exec_fn(*args)
            return _extract_exec(result)
        except TypeError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:
            raise RuntimeError(f"daytona process execution failed: {exc}") from exc

    raise RuntimeError(f"daytona process.exec signature mismatch: {'; '.join(errors)}")


def _fs_download(fs: Any, path: str) -> str:
    fn = getattr(fs, "download_file", None)
    if not callable(fn):
        raise RuntimeError("daytona sandbox fs.download_file is unavailable")

    for kwargs in ({"path": path}, {}):
        try:
            if kwargs:
                result = fn(**kwargs)
            else:
                result = fn(path)
            if isinstance(result, bytes):
                return result.decode("utf-8", errors="replace")
            if isinstance(result, bytearray):
                return bytes(result).decode("utf-8", errors="replace")
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                body = result.get("content") or result.get("data")
                if isinstance(body, (bytes, bytearray)):
                    return bytes(body).decode("utf-8", errors="replace")
                if body is not None:
                    return str(body)
            body = _entry_get(result, "content", "data")
            if isinstance(body, (bytes, bytearray)):
                return bytes(body).decode("utf-8", errors="replace")
            if body is not None:
                return str(body)
            return str(result)
        except TypeError:
            continue
    raise RuntimeError("daytona fs.download_file call failed")


def _fs_upload(fs: Any, path: str, content: str) -> None:
    fn = getattr(fs, "upload_file", None)
    if not callable(fn):
        raise RuntimeError("daytona sandbox fs.upload_file is unavailable")

    data = content.encode("utf-8")
    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        ((), {"path": path, "data": data}),
        ((), {"path": path, "content": data}),
        ((path, data), {}),
        ((path, content), {}),
    ]
    for args, kwargs in variants:
        try:
            fn(*args, **kwargs)
            return
        except TypeError:
            continue
    raise RuntimeError("daytona fs.upload_file call failed")


def _fs_mkdir(fs: Any, path: str) -> None:
    fn = getattr(fs, "create_folder", None)
    if not callable(fn):
        fn = getattr(fs, "mkdir", None)
    if not callable(fn):
        return
    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        ((), {"path": path}),
        ((path,), {}),
    ]
    for args, kwargs in variants:
        try:
            fn(*args, **kwargs)
            return
        except TypeError:
            continue
        except Exception:
            return


def _fs_list(fs: Any, path: str) -> list[Any]:
    fn = getattr(fs, "list_files", None)
    if not callable(fn):
        fn = getattr(fs, "list", None)
    if not callable(fn):
        return []

    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        ((), {"path": path}),
        ((path,), {}),
    ]
    for args, kwargs in variants:
        try:
            result = fn(*args, **kwargs)
            return _extract_items(result)
        except TypeError:
            continue
    return []


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


def _check_rg(ctx: DaytonaCtx, tool_ctx) -> bool:
    cached = ctx.manager.get_cached_rg_available(ctx.sandbox_id)
    if cached is not None:
        return cached

    code, out, _ = _process_exec(ctx.sandbox, "command -v rg", cwd=ctx.cwd, timeout_ms=10_000)
    if code == 0 and out.strip():
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
        for install_cmd in install_commands:
            code, _, _ = _process_exec(ctx.sandbox, install_cmd, cwd=ctx.cwd, timeout_ms=120_000)
            if code == 0:
                break

    code, out, _ = _process_exec(ctx.sandbox, "command -v rg", cwd=ctx.cwd, timeout_ms=10_000)
    if code == 0 and out.strip():
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

        code, out, err = _process_exec(self._ctx.sandbox, command, cwd=workdir, timeout_ms=timeout_ms)
        output = out or ""
        if err:
            output = f"{output}\n{err}".strip()
        t = truncate(output)
        await ctx.tool_stream_update(t.content)
        return ToolResult(
            title="bash",
            output=t.content,
            metadata={"returncode": code, "truncated": t.truncated, "workdir": workdir},
        )


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

        content = _fs_download(self._ctx.sandbox.fs, path)
        lines = content.splitlines()
        offset = int(args.get("offset") or 0)
        limit = int(args.get("limit") or 2000)
        chunk = lines[offset : offset + limit]
        text = "\n".join(chunk)
        t = truncate(text)
        return ToolResult(
            title=path,
            output=t.content,
            metadata={"truncated": t.truncated, "offset": offset, "limit": limit, "total_lines": len(lines)},
        )


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

        parent = posixpath.dirname(path)
        if parent and parent != "/":
            _fs_mkdir(self._ctx.sandbox.fs, parent)
        _fs_upload(self._ctx.sandbox.fs, path, content)
        return ToolResult(title=path, output="Wrote file successfully.", metadata={"file_path": path})


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

        files: list[str]
        if _check_rg(self._ctx, ctx):
            rg_cmd = f"rg --files --hidden --no-messages --glob {shlex.quote(pattern)} {shlex.quote(search_root)}"
            code, out, _ = _process_exec(self._ctx.sandbox, rg_cmd, cwd=self._ctx.cwd, timeout_ms=120_000)
            if code not in (0, 1):
                files = []
            else:
                files = [line.strip() for line in out.splitlines() if line.strip()]
        else:
            logger.warning(
                "fall back to glob emulation without ripgrep",
                event="daytona.glob.fallback_emulation",
                session_id=ctx.session_id,
                sandbox_id=self._ctx.sandbox_id,
            )
            all_files = _walk_files(self._ctx.sandbox.fs, search_root)
            files = [p for p in all_files if _match_glob(pattern, search_root, p)]

        files = sorted(dict.fromkeys(files))
        truncated = len(files) > _MAX_RESULTS
        final_files = files[:_MAX_RESULTS]
        output_lines: list[str] = final_files or ["No files found"]
        if truncated:
            output_lines += ["", "(Results are truncated. Consider using a more specific path or pattern.)"]

        return ToolResult(
            title=search_root,
            output="\n".join(output_lines),
            metadata={"count": len(final_files), "truncated": truncated},
        )


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

        matches: list[tuple[str, int, str]] = []
        if _check_rg(self._ctx, ctx):
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

        if not matches:
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
        return ToolResult(
            title=pattern,
            output=t.content,
            metadata={"matches": len(final), "truncated": truncated or t.truncated},
        )
