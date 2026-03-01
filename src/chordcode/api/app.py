from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from chordcode.bus.bus import Bus, Event
from chordcode.channels import ChannelBus, ChannelManager, ChannelSessionBridge
from chordcode.channels.events import InboundChannelMessage, OutboundChannelMessage
from chordcode.config import load, config_to_dict, mask_sensitive, save_config, generate_default_yaml, get_config_sources, project_config_paths, GLOBAL_CONFIG_PATHS, _load_yaml_file, _deep_merge
from chordcode.config_schema import CONFIG_FIELD_META
from chordcode.cron import CronJobExecResult, CronService
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker, loghook
from chordcode.llm.openai_chat import OpenAIChatProvider
from chordcode.loop.interrupt import InterruptManager
from chordcode.loop.session_loop import SessionLoop
from chordcode.log import init_logging, logger
from chordcode.model import (
    AddMessageRequest, CreateCronJobRequest, CronJob, CronJobEnabledRequest, CronJobRunRequest, CreateSessionRequest, Message, ModelRef,
    PermissionReply, PermissionRule, RenameSessionRequest, Session, SessionRuntime, DaytonaRuntimeConfig, TextPart,
)
from chordcode.observability.langfuse_client import init_langfuse, flush_langfuse, shutdown_langfuse
from chordcode.observability.langfuse_hook import create_langfuse_hook
from chordcode.permission.service import PermissionService
from chordcode.runtime import DaytonaManager, DaytonaOperationError, DaytonaUnavailableError
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.bash import BashCtx, BashTool
from chordcode.tools.daytona import DaytonaBashTool, DaytonaCtx, DaytonaGlobTool, DaytonaGrepTool, DaytonaReadTool, DaytonaWriteTool
from chordcode.tools.files import FileCtx, ReadTool, WriteTool
from chordcode.tools.grep import GlobTool, GrepTool, SearchCtx
from chordcode.tools.skill import SkillCtx, SkillTool
from chordcode.tools.todo import TodoWriteTool
from chordcode.tools.registry import ToolRegistry
from chordcode.tools.web import TavilySearchTool, WebFetchTool, WebSearchCtx
from chordcode.tools.kb_search import KBSearchCtx, KBSearchTool
from chordcode.mcp import MCPManager, MCPToolAdapter, load_mcp_configs, MCPServerConfig
from chordcode.skills.loader import SkillLoader

cfg = load()
init_logging(
    level=cfg.logging.level,
    console=cfg.logging.console,
    file=cfg.logging.file,
    log_dir=cfg.logging.dir,
    rotation=cfg.logging.rotation,
    retention=cfg.logging.retention,
)
logger.info("Chord Code starting", event="app.start", model=cfg.openai.model, openai_base_url=cfg.openai.base_url)
bus = Bus()
store = SQLiteStore(cfg.db_path)
hooks = Hooker()
lh = loghook(cfg=cfg)
if lh:
    hooks.add(lh)

# Initialize Langfuse tracing
init_langfuse(cfg.langfuse)

# Add Langfuse hook
langfuse_hook = create_langfuse_hook()
if langfuse_hook:
    hooks.add(langfuse_hook)

perm = PermissionService(bus, store, hooks)
mcp_manager = MCPManager(bus=bus)
daytona_manager = DaytonaManager(cfg.daytona, store)

# Initialize KB backend (optional — disabled when base_url is empty)
kb_client = None
vlm_client = None

if cfg.kb.base_url:
    from chordcode.kb.lightrag_client import LightRAGClient
    kb_client = LightRAGClient(base_url=cfg.kb.base_url, api_key=cfg.kb.api_key)
    logger.info("KB backend enabled", event="kb.init", backend=cfg.kb.backend, base_url=cfg.kb.base_url)

if cfg.vlm.backend == "paddleocr" and cfg.vlm.api_url:
    from chordcode.kb.paddleocr_client import PaddleOCRClient
    vlm_client = PaddleOCRClient(
        api_url=cfg.vlm.api_url, api_key=cfg.vlm.api_key,
        poll_interval=cfg.vlm.poll_interval, timeout=cfg.vlm.timeout,
    )
    logger.info("VLM parser enabled", event="vlm.init", backend=cfg.vlm.backend)

llm = OpenAIChatProvider(
    base_url=cfg.openai.base_url,
    api_key=cfg.openai.api_key,
    model=cfg.openai.model,
    langfuse_enabled=cfg.langfuse.enabled,
)
interrupt = InterruptManager()
channel_bus = ChannelBus()
channel_manager = ChannelManager(cfg, channel_bus)
channel_bridge: ChannelSessionBridge | None = None
cron_service: CronService | None = None
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOG_FILE_RE = re.compile(r"^chordcode_(\d{4}-\d{2}-\d{2})\.jsonl$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _default_rules() -> list[PermissionRule]:
    return [
        PermissionRule(permission="*", pattern="*", action=cfg.default_permission_action),
    ]


def _build_feishu_channel_rules(runtime_cfg: Any) -> list[PermissionRule]:
    feishu_cfg = runtime_cfg.channels.feishu
    mode = str(getattr(feishu_cfg, "permission_mode", "deny") or "deny").strip().lower()
    allowed_cmds = [str(x).strip() for x in (getattr(feishu_cfg, "allowed_bash_commands", []) or []) if str(x).strip()]

    if mode == "allow":
        return [PermissionRule(permission="*", pattern="*", action="allow")]

    if mode == "commands":
        rules: list[PermissionRule] = [
            PermissionRule(permission="bash", pattern=cmd, action="allow")
            for cmd in allowed_cmds
        ]
        rules.append(PermissionRule(permission="*", pattern="*", action="deny"))
        return rules

    return [PermissionRule(permission="*", pattern="*", action="deny")]


def _same_rules(a: list[PermissionRule], b: list[PermissionRule]) -> bool:
    return [r.model_dump() for r in a] == [r.model_dump() for r in b]


async def _get_session_or_404(session_id: str) -> Session:
    try:
        return await store.get_session(session_id)
    except KeyError as e:
        detail = str(e.args[0]) if e.args else "session not found"
        raise HTTPException(status_code=404, detail=detail)


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


async def _add_user_message(session: Session, text: str, *, source: str = "api") -> str:
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    now = int(time.time() * 1000)
    msg = Message(
        id=str(uuid4()),
        session_id=session.id,
        role="user",
        parent_id=None,
        agent="primary",
        model=ModelRef(provider="openai-compatible", id=cfg.openai.model),
        created_at=now,
    )
    out: dict[str, object] = {"text": text}
    await hooks.trigger(Hook.ChatMessage, {"session_id": session.id, "agent": msg.agent, "message_id": msg.id}, out)
    text = str(out.get("text") or text)

    await store.add_message(msg)
    text_part = TextPart(id=str(uuid4()), message_id=msg.id, session_id=session.id, text=text)
    await store.add_part(session.id, msg.id, text_part)
    await store.touch_session(session.id)

    await bus.publish(Event(type="message.updated", properties={"session_id": session.id, "info": msg.model_dump()}))
    await bus.publish(
        Event(
            type="message.part.updated",
            properties={"session_id": session.id, "message_id": msg.id, "part": text_part.model_dump(), "delta": text},
        ),
    )
    logger.info(
        "User message stored",
        event="message.user.added",
        session_id=session.id,
        message_id=msg.id,
        source=source,
        content_chars=len(text),
    )
    return msg.id


async def _build_tools(session: Session) -> ToolRegistry:
    runtime_tools: list[Any]
    if session.runtime.backend == "daytona":
        sandbox_ref = await daytona_manager.get_sandbox_for_session(session)
        daytona_ctx = DaytonaCtx(
            worktree=session.worktree,
            cwd=session.cwd,
            sandbox=sandbox_ref.sandbox,
            sandbox_id=sandbox_ref.sandbox_id,
            manager=daytona_manager,
        )
        runtime_tools = [
            DaytonaBashTool(daytona_ctx),
            DaytonaReadTool(daytona_ctx),
            DaytonaGlobTool(daytona_ctx),
            DaytonaGrepTool(daytona_ctx),
            DaytonaWriteTool(daytona_ctx),
        ]
    else:
        runtime_tools = [
            BashTool(BashCtx(worktree=session.worktree, cwd=session.cwd)),
            ReadTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
            GlobTool(SearchCtx(worktree=session.worktree, cwd=session.cwd)),
            GrepTool(SearchCtx(worktree=session.worktree, cwd=session.cwd)),
            WriteTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
        ]

    builtin_tools = runtime_tools + [
        TavilySearchTool(WebSearchCtx(tavily_api_key=cfg.web_search.tavily_api_key)),
        WebFetchTool(),
        SkillTool(SkillCtx(worktree=session.worktree, cwd=session.cwd, permission_rules=session.permission_rules)),
        TodoWriteTool(store=store, bus=bus),
    ]

    if kb_client is not None:
        builtin_tools.append(KBSearchTool(KBSearchCtx(kb=kb_client)))

    mcp_tool_infos = await mcp_manager.list_tools()
    mcp_tools = [MCPToolAdapter(info, mcp_manager) for info in mcp_tool_infos]
    return ToolRegistry(builtin_tools + mcp_tools)


async def _run_agent_once(session: Session, *, source: str = "api") -> tuple[str, str | None]:
    lock = await _get_session_lock(session.id)
    async with lock:
        tools = await _build_tools(session)
        loop = SessionLoop(cfg=cfg, bus=bus, store=store, perm=perm, tools=tools, llm=llm, interrupt=interrupt, hooks=hooks)
        logger.info("Running session loop", event="session.run.requested", session_id=session.id, source=source)
        return await loop.run(session_id=session.id, source=source)


def _require_cron_service() -> CronService:
    if not cron_service:
        raise HTTPException(status_code=503, detail="cron service not ready")
    return cron_service


async def _execute_cron_job(job: CronJob) -> CronJobExecResult | None:
    session = await store.get_session(job.session_id)
    await _add_user_message(session, job.payload.message, source=f"cron:{job.id}")
    assistant_message_id, trace_id = await _run_agent_once(session, source=f"cron:{job.id}")
    return CronJobExecResult(assistant_message_id=assistant_message_id, trace_id=trace_id)


async def _extract_assistant_text(session_id: str, message_id: str) -> str:
    history = await store.list_messages(session_id)
    target = next((m for m in history if m.info.id == message_id), None)
    if not target:
        return ""
    chunks: list[str] = []
    for part in target.parts:
        if getattr(part, "type", None) == "text":
            text = getattr(part, "text", "")
            if text:
                chunks.append(text)
    return "".join(chunks).strip()


async def _resolve_or_create_channel_session(msg: InboundChannelMessage) -> Session:
    runtime_cfg = _load_effective_config()
    bound_session_id = await store.get_channel_session(msg.channel, msg.chat_id)
    if bound_session_id:
        try:
            session = await store.get_session(bound_session_id)
            desired_rules = _build_feishu_channel_rules(runtime_cfg)
            if not _same_rules(session.permission_rules, desired_rules):
                session = await store.update_session_permission_rules(bound_session_id, desired_rules)
                logger.info(
                    "Channel session permission rules synchronized",
                    event="channel.session.rules.adjusted",
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_id=bound_session_id,
                    permission_mode=runtime_cfg.channels.feishu.permission_mode,
                )
            return session
        except KeyError:
            logger.warning(
                "Channel session binding points to missing session; recreating",
                event="channel.session.binding_stale",
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_id=bound_session_id,
            )

    now = int(time.time() * 1000)
    title = f"[{msg.channel}] {msg.chat_id}"
    s = Session(
        id=str(uuid4()),
        title=title,
        worktree=runtime_cfg.default_worktree,
        cwd=runtime_cfg.default_worktree,
        created_at=now,
        updated_at=now,
        permission_rules=_build_feishu_channel_rules(runtime_cfg),
        runtime=SessionRuntime(backend="local"),
    )
    await store.create_session(s)
    await store.bind_channel_session(
        channel=msg.channel,
        chat_id=msg.chat_id,
        session_id=s.id,
        sender_id=msg.sender_id,
    )
    await bus.publish(Event(type="session.created", properties={"session_id": s.id, "info": s.model_dump()}))
    logger.info(
        "Channel session created",
        event="channel.session.created",
        channel=msg.channel,
        chat_id=msg.chat_id,
        sender_id=msg.sender_id,
        session_id=s.id,
    )
    return s


async def _process_channel_inbound(msg: InboundChannelMessage) -> OutboundChannelMessage | None:
    session = await _resolve_or_create_channel_session(msg)
    text = msg.content.strip()
    if not text:
        return None

    await _add_user_message(session, text, source=f"channel:{msg.channel}")
    assistant_message_id, trace_id = await _run_agent_once(session, source=f"channel:{msg.channel}")
    content = await _extract_assistant_text(session.id, assistant_message_id)
    if not content:
        history = await store.list_messages(session.id)
        assistant = next((m.info for m in history if m.info.id == assistant_message_id), None)
        if assistant and assistant.finish == "blocked":
            content = (
                "工具调用被权限策略拦截。请在 Channel Config 里调整 permission_mode（allow / commands）并配置可执行命令后重试。"
            )
        else:
            content = "(empty response)"

    metadata: dict[str, Any] = {
        "session_id": session.id,
        "assistant_message_id": assistant_message_id,
        "source": f"channel:{msg.channel}",
    }
    if trace_id:
        metadata["trace_id"] = trace_id
        metadata["trace_url"] = f"{cfg.langfuse.base_url}/trace/{trace_id}"

    return OutboundChannelMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=content,
        metadata=metadata,
    )


def _load_effective_config() -> Any:
    """Load latest merged config from disk (global + project)."""
    return load(cfg.default_worktree)


def _parse_allow_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.replace("\r", "\n")
        items: list[str] = []
        for line in text.split("\n"):
            for token in line.split(","):
                s = token.strip()
                if s:
                    items.append(s)
        return items
    return []


def _parse_line_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.replace("\r", "\n")
        items: list[str] = []
        for line in text.split("\n"):
            for token in line.split(","):
                s = token.strip()
                if s:
                    items.append(s)
        return items
    return []


def _serialize_feishu_config(channel_cfg: Any) -> dict[str, Any]:
    return {
        "enabled": bool(channel_cfg.enabled),
        "app_id": channel_cfg.app_id or "",
        "app_secret_set": bool(channel_cfg.app_secret),
        "encrypt_key_set": bool(channel_cfg.encrypt_key),
        "verification_token_set": bool(channel_cfg.verification_token),
        "allow_from": list(channel_cfg.allow_from or []),
        "permission_mode": str(getattr(channel_cfg, "permission_mode", "deny") or "deny"),
        "allowed_bash_commands": list(getattr(channel_cfg, "allowed_bash_commands", []) or []),
    }


async def _reload_channels_runtime(runtime_cfg: Any) -> None:
    global channel_manager
    old_manager = channel_manager
    await old_manager.stop_all()
    channel_manager = ChannelManager(runtime_cfg, channel_bus)
    await channel_manager.start_all()


app = FastAPI(title="Chord Code", version="0.1.0")

_web_dir = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=str(_web_dir)), name="static")


@app.on_event("startup")
async def _startup():
    global channel_bridge, cron_service
    await store.init()
    await hooks.trigger(Hook.Config, {"config": cfg}, {})

    # Initialize MCP servers
    mcp_configs = load_mcp_configs(cfg.default_worktree)
    if mcp_configs:
        await mcp_manager.initialize(mcp_configs)

    async def forward():
        async for e in bus.subscribe("*"):
            await hooks.trigger(Hook.Event, {"event": e}, {})

    asyncio.create_task(forward())

    # Start channel adapters and channel->session bridge
    await channel_manager.start_all()
    channel_bridge = ChannelSessionBridge(bus=channel_bus, process_inbound=_process_channel_inbound)
    await channel_bridge.start()

    cron_service = CronService(store=store, on_job=_execute_cron_job)
    await cron_service.start()


@app.on_event("shutdown")
async def _shutdown():
    """Shutdown handler to flush Langfuse events and close MCP connections."""
    global cron_service
    if cron_service:
        await cron_service.stop()
        cron_service = None
    if channel_bridge:
        await channel_bridge.stop()
    await channel_manager.stop_all()
    await mcp_manager.shutdown()
    shutdown_langfuse()

@app.get("/")
async def home():
    return FileResponse(_web_dir / "index.html")

@app.get("/config")
async def get_config():
    return mask_sensitive(config_to_dict(cfg))


@app.get("/config/schema")
async def get_config_schema():
    """Return field metadata for the Settings UI."""
    return {
        key: {
            "key": meta.key,
            "description": meta.description,
            "default": meta.default,
            "sensitive": meta.sensitive,
            "choices": meta.choices,
            "type": meta.type_name,
        }
        for key, meta in CONFIG_FIELD_META.items()
    }


@app.get("/config/sources")
async def get_config_sources_endpoint():
    return {"sources": get_config_sources(cfg.default_worktree)}


@app.get("/config/raw")
async def get_config_raw(scope: str = "project"):
    if scope not in ("project", "global"):
        raise HTTPException(status_code=400, detail="scope must be 'project' or 'global'")

    if scope == "global":
        paths = [Path(p).expanduser() for p in GLOBAL_CONFIG_PATHS]
    else:
        paths = [Path(p) for p in project_config_paths(cfg.default_worktree)]

    for p in paths:
        if p.is_file():
            return {"scope": scope, "path": str(p), "content": p.read_text(encoding="utf-8"), "exists": True}

    # No file found — return empty
    preferred = str(paths[0]) if paths else ""
    return {"scope": scope, "path": preferred, "content": "", "exists": False}


@app.put("/config/raw")
async def put_config_raw(body: dict[str, Any]):
    scope = body.get("scope", "project")
    content = body.get("content", "")
    if scope not in ("project", "global"):
        raise HTTPException(status_code=400, detail="scope must be 'project' or 'global'")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")

    # Validate YAML syntax
    try:
        import yaml as _yaml
        parsed = _yaml.safe_load(content)
        if content.strip() and not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping (dict)")
    except _yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if scope == "global":
        path = Path(GLOBAL_CONFIG_PATHS[0]).expanduser()
    else:
        paths = project_config_paths(cfg.default_worktree)
        path = Path(paths[0]) if paths else None
        if not path:
            raise HTTPException(status_code=400, detail="No project worktree configured")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path), "restart_required": True}


@app.patch("/config")
async def patch_config(body: dict[str, Any]):
    """Partial update: merge into existing project config file."""
    paths = project_config_paths(cfg.default_worktree)
    if not paths:
        raise HTTPException(status_code=400, detail="No project worktree configured")

    config_path = paths[0]  # prefer YAML
    existing = _load_yaml_file(config_path) or {}
    merged = _deep_merge(existing, body)
    save_config(merged, config_path)
    return {"ok": True, "path": config_path, "restart_required": True}


@app.post("/config/init")
async def init_config(body: dict[str, Any]):
    """Generate a default config file at the specified scope."""
    scope = body.get("scope", "project")
    if scope not in ("project", "global"):
        raise HTTPException(status_code=400, detail="scope must be 'project' or 'global'")

    if scope == "global":
        path = Path(GLOBAL_CONFIG_PATHS[0]).expanduser()
    else:
        paths = project_config_paths(cfg.default_worktree)
        if not paths:
            raise HTTPException(status_code=400, detail="No project worktree configured")
        path = Path(paths[0])

    if path.is_file():
        raise HTTPException(status_code=409, detail=f"Config file already exists: {path}")

    content = generate_default_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def _resolve_log_dir() -> Path:
    raw = cfg.logging.dir or "./data/logs"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def _validate_date(value: str) -> str:
    date = value.strip()
    if not _DATE_RE.fullmatch(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return date


def _list_log_files(log_dir: Path) -> list[dict[str, object]]:
    if not log_dir.exists() or not log_dir.is_dir():
        return []
    files: list[dict[str, object]] = []
    for p in log_dir.iterdir():
        if not p.is_file():
            continue
        m = _LOG_FILE_RE.fullmatch(p.name)
        if not m:
            continue
        stat = p.stat()
        files.append(
            {
                "date": m.group(1),
                "name": p.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime * 1000),
            },
        )
    files.sort(key=lambda x: str(x["date"]), reverse=True)
    return files


def _log_file_for_date(log_dir: Path, date: str) -> Path:
    return log_dir / f"chordcode_{date}.jsonl"


@app.get("/logs/files")
async def list_log_files():
    log_dir = _resolve_log_dir()
    files = _list_log_files(log_dir)
    return {
        "log_dir": str(log_dir),
        "files": files,
        "default_date": files[0]["date"] if files else None,
    }


@app.get("/logs")
async def list_logs(
    date: str,
    offset: int = 0,
    limit: int = 100,
    level: str | None = None,
    event: str | None = None,
    session_id: str | None = None,
    q: str | None = None,
):
    date = _validate_date(date)
    if offset < 0:
        offset = 0
    limit = max(1, min(limit, 500))

    level_norm = (level or "").strip().upper()
    if level_norm and level_norm not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail="level must be one of DEBUG, INFO, WARNING, ERROR")
    event_norm = (event or "").strip().lower()
    session_norm = (session_id or "").strip()
    q_norm = (q or "").strip().lower()

    log_dir = _resolve_log_dir()
    path = _log_file_for_date(log_dir, date)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"log file not found for date {date}")

    matched: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line_text = line.strip()
            if not line_text:
                continue
            try:
                raw_obj = json.loads(line_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw_obj, dict):
                continue

            item_level = str(raw_obj.get("level") or "").upper()
            item_event = str(raw_obj.get("event") or "")
            item_session = str(raw_obj.get("session_id") or "")
            item_text = json.dumps(raw_obj, ensure_ascii=False, separators=(",", ":")).lower()

            if level_norm and item_level != level_norm:
                continue
            if event_norm and event_norm not in item_event.lower():
                continue
            if session_norm and item_session != session_norm:
                continue
            if q_norm and q_norm not in item_text:
                continue

            matched.append(
                {
                    "line_no": i,
                    "ts": raw_obj.get("ts"),
                    "level": raw_obj.get("level"),
                    "event": raw_obj.get("event"),
                    "session_id": raw_obj.get("session_id"),
                    "message": raw_obj.get("message"),
                    "module": raw_obj.get("module"),
                    "function": raw_obj.get("function"),
                    "raw": raw_obj,
                },
            )

    matched.reverse()  # newest first
    total = len(matched)
    page = matched[offset : offset + limit]
    return {
        "date": date,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": (offset + limit) < total,
        "items": page,
    }


@app.post("/sessions")
async def create_session(body: CreateSessionRequest):
    requested_runtime = body.runtime or SessionRuntime(backend="local")
    runtime_backend = requested_runtime.backend

    raw_worktree = body.worktree.strip()
    title = body.title.strip() or "New session"
    if runtime_backend == "daytona":
        default_workspace = cfg.daytona.default_workspace or "/workspace"
        worktree = raw_worktree or default_workspace
        cwd = body.cwd.strip() or worktree
        if not worktree.startswith("/") or not cwd.startswith("/"):
            raise HTTPException(status_code=400, detail="daytona worktree/cwd must be absolute remote paths")
        runtime = SessionRuntime(
            backend="daytona",
            daytona=DaytonaRuntimeConfig(
                sandbox_id=requested_runtime.daytona.sandbox_id if requested_runtime.daytona else None,
            ),
        )
    else:
        worktree = raw_worktree
        if not worktree or not os.path.isabs(worktree):
            raise HTTPException(status_code=400, detail="worktree must be an absolute path")
        cwd = body.cwd.strip() or worktree
        runtime = SessionRuntime(backend="local")

    permission_rules = [PermissionRule.model_validate(x) for x in body.permission_rules] if body.permission_rules else _default_rules()
    now = int(time.time() * 1000)
    s = Session(
        id=str(uuid4()),
        title=title,
        worktree=worktree,
        cwd=cwd,
        created_at=now,
        updated_at=now,
        permission_rules=permission_rules,
        runtime=runtime,
    )
    await store.create_session(s)
    if runtime_backend == "daytona":
        try:
            s = await daytona_manager.ensure_session_runtime_async(s)
        except DaytonaUnavailableError as exc:
            await store.delete_session(s.id)
            raise HTTPException(status_code=503, detail=str(exc))
        except DaytonaOperationError as exc:
            await store.delete_session(s.id)
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(status_code=400, detail=msg)
            raise HTTPException(status_code=502, detail=msg)

    await bus.publish(Event(type="session.created", properties={"session_id": s.id, "info": s.model_dump()}))
    return s


@app.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    """List all sessions, ordered by updated_at desc."""
    sessions = await store.list_sessions(limit=limit, offset=offset)
    return {"sessions": [s.model_dump() for s in sessions]}


@app.patch("/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameSessionRequest):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    await _get_session_or_404(session_id)
    session = await store.update_session_title(session_id, title)
    await bus.publish(Event(type="session.updated", properties={"session_id": session_id, "info": session.model_dump()}))
    return session


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    session = await _get_session_or_404(session_id)
    await store.delete_session(session_id)
    await bus.publish(
        Event(
            type="session.deleted",
            properties={"session_id": session_id, "info": session.model_dump()},
        ),
    )
    return {"ok": True, "session_id": session_id}


@app.post("/sessions/{session_id}/messages")
async def add_message(session_id: str, body: AddMessageRequest):
    session = await store.get_session(session_id)
    message_id = await _add_user_message(session, body.text, source="api")
    return {"message_id": message_id}


@app.post("/sessions/{session_id}/run")
async def run_session(session_id: str):
    session = await store.get_session(session_id)
    try:
        msg_id, trace_id = await _run_agent_once(session, source="api")
    except DaytonaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except DaytonaOperationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    result = {"assistant_message_id": msg_id}
    if trace_id:
        result["trace_id"] = trace_id
        result["trace_url"] = f"{cfg.langfuse.base_url}/trace/{trace_id}"
    return result


@app.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    """Interrupt a running session."""
    await store.get_session(session_id)  # Validate session exists
    await interrupt.interrupt(session_id, reason="user_cancelled")
    await bus.publish(
        Event(
            type="session.interrupted",
            properties={"session_id": session_id, "reason": "user_cancelled"},
        ),
    )
    return {"ok": True}

@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    return await store.get_session(session_id)


@app.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str):
    await store.get_session(session_id)
    return await store.list_messages(session_id)


@app.get("/sessions/{session_id}/todos")
async def get_todos(session_id: str):
    """Get the current todo list for a session."""
    await store.get_session(session_id)  # Validate session exists
    todos = await store.get_todos(session_id)
    return {"session_id": session_id, "todos": [t.model_dump() for t in todos]}


@app.post("/cronjobs")
async def create_cron_job(body: CreateCronJobRequest):
    service = _require_cron_service()
    await _get_session_or_404(body.session_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    now = int(time.time() * 1000)
    job = CronJob(
        id=str(uuid4()),
        name=name,
        session_id=body.session_id,
        enabled=body.enabled,
        schedule=body.schedule,
        payload={"kind": "agent_turn", "message": body.message},
        created_at_ms=now,
        updated_at_ms=now,
        delete_after_run=body.delete_after_run,
    )
    try:
        return await service.create_job(job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/cronjobs")
async def list_cron_jobs(session_id: str | None = None, include_disabled: bool = True):
    jobs = await store.list_cron_jobs(session_id=session_id, include_disabled=include_disabled)
    return {"jobs": [j.model_dump() for j in jobs]}


@app.get("/cronjobs/status")
async def cron_status():
    service = _require_cron_service()
    return await service.status()


@app.get("/cronjobs/{job_id}")
async def get_cron_job(job_id: str):
    try:
        return await store.get_cron_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))


@app.delete("/cronjobs/{job_id}")
async def delete_cron_job(job_id: str):
    service = _require_cron_service()
    deleted = await service.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"cron job not found: {job_id}")
    return {"ok": True, "job_id": job_id}


@app.post("/cronjobs/{job_id}/enabled")
async def set_cron_job_enabled(job_id: str, body: CronJobEnabledRequest):
    service = _require_cron_service()
    try:
        return await service.set_job_enabled(job_id, enabled=body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/cronjobs/{job_id}/run")
async def run_cron_job(job_id: str, body: CronJobRunRequest | None = None):
    service = _require_cron_service()
    force = bool(body.force) if body else False
    try:
        ran = await service.run_job(job_id, force=force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))
    if not ran:
        raise HTTPException(status_code=409, detail="cron job is disabled; pass force=true to run manually")
    return {"ok": True, "job_id": job_id}


@app.get("/cronjobs/{job_id}/runs")
async def list_cron_job_runs(job_id: str, limit: int = 50):
    try:
        await store.get_cron_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))
    runs = await store.list_cron_job_runs(job_id, limit=max(1, min(limit, 200)))
    return {"job_id": job_id, "runs": [r.model_dump() for r in runs]}


@app.get("/permissions/pending")
async def pending_permissions(session_id: str):
    await store.get_session(session_id)
    return await store.list_pending_permission_requests(session_id)


@app.get("/events")
async def events(session_id: str | None = None):
    async def gen():
        async for e in bus.subscribe("*"):
            sid = e.properties.get("session_id") or (e.properties.get("info") or {}).get("session_id")
            if session_id and sid and sid != session_id:
                continue
            yield f"data: {json.dumps({'type': e.type, 'properties': e.properties})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/channels/status")
async def channels_status():
    runtime_cfg = _load_effective_config()
    return {
        "enabled_channels": channel_manager.enabled_channels,
        "channels": channel_manager.get_status(),
        "configured": {
            "feishu": _serialize_feishu_config(runtime_cfg.channels.feishu),
        },
        "bridge_running": bool(channel_bridge and channel_bridge.is_running),
        "queue": {
            "inbound": channel_bus.inbound_size,
            "outbound": channel_bus.outbound_size,
        },
    }


@app.get("/channels/config")
async def channels_config():
    runtime_cfg = _load_effective_config()
    return {
        "channels": {
            "feishu": _serialize_feishu_config(runtime_cfg.channels.feishu),
        }
    }


@app.put("/channels/config/feishu")
async def update_feishu_channel_config(body: dict[str, Any]):
    paths = project_config_paths(cfg.default_worktree)
    if not paths:
        raise HTTPException(status_code=400, detail="No project worktree configured")

    existing_cfg = _load_effective_config()
    existing = _load_yaml_file(paths[0]) or {}
    existing_feishu = existing_cfg.channels.feishu

    keep_existing_secret = bool(body.get("keep_existing_secret", True))
    app_secret = str(body.get("app_secret", "") or "").strip()
    encrypt_key = str(body.get("encrypt_key", "") or "").strip()
    verification_token = str(body.get("verification_token", "") or "").strip()

    if keep_existing_secret:
        if not app_secret:
            app_secret = existing_feishu.app_secret
        if not encrypt_key:
            encrypt_key = existing_feishu.encrypt_key
        if not verification_token:
            verification_token = existing_feishu.verification_token

    permission_mode_raw = str(
        body.get("permission_mode", getattr(existing_feishu, "permission_mode", "deny")) or "deny"
    ).strip().lower()
    if permission_mode_raw not in ("deny", "allow", "commands"):
        raise HTTPException(status_code=400, detail="permission_mode must be one of: deny, allow, commands")

    feishu_patch = {
        "enabled": bool(body.get("enabled", existing_feishu.enabled)),
        "app_id": str(body.get("app_id", existing_feishu.app_id) or "").strip(),
        "app_secret": app_secret,
        "encrypt_key": encrypt_key,
        "verification_token": verification_token,
        "allow_from": _parse_allow_from(body.get("allow_from", existing_feishu.allow_from)),
        "permission_mode": permission_mode_raw,
        "allowed_bash_commands": _parse_line_list(
            body.get("allowed_bash_commands", getattr(existing_feishu, "allowed_bash_commands", []))
        ),
    }

    merged = _deep_merge(existing, {"channels": {"feishu": feishu_patch}})
    save_config(merged, paths[0])

    runtime_cfg = _load_effective_config()
    await _reload_channels_runtime(runtime_cfg)
    logger.info(
        "Feishu channel config updated",
        event="channel.config.updated",
        channel="feishu",
        enabled=feishu_patch["enabled"],
        allow_from_count=len(feishu_patch["allow_from"]),
        permission_mode=feishu_patch["permission_mode"],
        allowed_bash_commands_count=len(feishu_patch["allowed_bash_commands"]),
    )
    return {
        "ok": True,
        "restart_required": False,
        "channels": {"feishu": _serialize_feishu_config(runtime_cfg.channels.feishu)},
        "runtime_status": channel_manager.get_status(),
    }


@app.post("/channels/{name}/connect")
async def connect_channel(name: str):
    try:
        await channel_manager.connect_channel(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")
    return {"ok": True, "channel": name, "status": channel_manager.get_status().get(name, {})}


@app.post("/channels/{name}/disconnect")
async def disconnect_channel(name: str):
    try:
        await channel_manager.disconnect_channel(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")
    return {"ok": True, "channel": name, "status": channel_manager.get_status().get(name, {})}


@app.post("/channels/{name}/test")
async def test_channel(name: str):
    try:
        result = await channel_manager.test_channel(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")
    return result


@app.post("/permissions/{request_id}/reply")
async def reply_permission(request_id: str, body: PermissionReply):
    await perm.reply(request_id, body)
    return {"ok": True}


# -- Skills endpoints --

@app.get("/skills")
async def list_skills(worktree: str | None = None):
    wt = (worktree or "").strip() or cfg.default_worktree
    loader = SkillLoader(worktree=wt, cwd=wt)
    skills = loader.list_skills()
    return {
        "worktree": wt,
        "skills": [
            {"name": s.name, "description": s.description, "path": s.path, "dir": s.dir}
            for s in skills
        ],
    }


@app.get("/skills/{name}")
async def get_skill(name: str, worktree: str | None = None):
    wt = (worktree or "").strip() or cfg.default_worktree
    loader = SkillLoader(worktree=wt, cwd=wt)
    skill = loader.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    files = loader.sample_files(name)
    return {
        "name": skill.name,
        "description": skill.description,
        "path": skill.path,
        "dir": skill.dir,
        "body": skill.body,
        "files": files,
    }


# -- MCP endpoints --

@app.get("/mcp/status")
async def mcp_status():
    statuses = mcp_manager.status()
    return {"servers": [
        {"name": s.name, "status": s.status, "error": s.error, "tool_count": s.tool_count}
        for s in statuses
    ]}


@app.get("/mcp/tools")
async def mcp_tools():
    tools = await mcp_manager.list_tools()
    return {"tools": [
        {
            "server": t.server_name,
            "name": t.tool_name,
            "namespaced_name": t.namespaced_name,
            "description": t.description,
        }
        for t in tools
    ]}


@app.post("/mcp/{name}/connect")
async def mcp_connect(name: str):
    try:
        await mcp_manager.connect(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown MCP server: {name}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.post("/mcp/{name}/disconnect")
async def mcp_disconnect(name: str):
    await mcp_manager.disconnect(name)
    return {"ok": True}


@app.post("/mcp/servers")
async def mcp_add_server(body: dict[str, Any]):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    config_raw = body.get("config", {})
    if not isinstance(config_raw, dict):
        raise HTTPException(status_code=400, detail="config must be a dict")

    # Determine type from config
    command = config_raw.get("command", "")
    url = config_raw.get("url", "")
    if not command and not url:
        raise HTTPException(status_code=400, detail="config must have 'command' or 'url'")

    if command:
        cfg_obj = MCPServerConfig(
            name=name, type="local", command=command,
            args=list(config_raw.get("args", [])),
            env=dict(config_raw.get("env", {})),
            enabled=config_raw.get("enabled", True),
            timeout=int(config_raw.get("timeout", 30)),
            source="api",
            transport="stdio",
        )
    else:
        transport = config_raw.get("transport", "streamable-http")
        if transport not in ("stdio", "sse", "streamable-http"):
            transport = "streamable-http"
        cfg_obj = MCPServerConfig(
            name=name, type="remote", url=url,
            headers=dict(config_raw.get("headers", {})),
            enabled=config_raw.get("enabled", True),
            timeout=int(config_raw.get("timeout", 30)),
            source="api",
            transport=transport,
        )

    try:
        await mcp_manager.add_server(name, cfg_obj)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "name": name}


# -- KB endpoints --

def _require_kb():
    if kb_client is None:
        raise HTTPException(status_code=404, detail="Knowledge base is not configured")
    return kb_client


@app.get("/kb/config")
async def kb_config():
    logger.debug("KB config requested", event="api.kb.config")
    return {
        "enabled": kb_client is not None,
        "backend": cfg.kb.backend if kb_client else "none",
        "vlm_available": vlm_client is not None,
        "vlm_backend": cfg.vlm.backend if vlm_client else "none",
    }


@app.post("/kb/documents/upload")
async def kb_upload_file(file: UploadFile, use_vlm: bool = False):
    kb = _require_kb()
    file_bytes = await file.read()
    filename = file.filename or "upload"
    logger.info("API: KB upload file", event="api.kb.upload", filename=filename, size=len(file_bytes), use_vlm=use_vlm)

    if use_vlm:
        if vlm_client is None:
            raise HTTPException(status_code=400, detail="VLM parser is not configured")
        job_id = await vlm_client.submit(file_bytes, filename)
        parse_result = await vlm_client.wait_for_result(job_id)
        text = "\n\n".join(parse_result.markdown_pages)
        logger.info("API: VLM parse done for upload", event="api.kb.upload.vlm_done", filename=filename, pages=len(parse_result.markdown_pages))
        # Upload the VLM-parsed markdown as a .md file
        md_filename = filename.rsplit(".", 1)[0] + ".md" if "." in filename else filename + ".md"
        file_bytes = text.encode("utf-8")
        filename = md_filename

    result = await kb.upload_file(file_bytes, filename)

    return result.model_dump()


@app.delete("/kb/documents")
async def kb_delete_documents(body: dict[str, Any]):
    kb = _require_kb()
    doc_ids = body.get("doc_ids", [])
    if not doc_ids or not isinstance(doc_ids, list):
        raise HTTPException(status_code=400, detail="doc_ids list is required")
    logger.info("API: KB delete documents", event="api.kb.delete", count=len(doc_ids))
    return await kb.delete_documents(doc_ids)


@app.post("/kb/documents/list")
async def kb_list_documents(body: dict[str, Any]):
    kb = _require_kb()
    page = int(body.get("page", 1))
    page_size = int(body.get("page_size", 20))
    status_filter = body.get("status_filter")
    logger.debug("API: KB list documents", event="api.kb.list", page=page, page_size=page_size, status_filter=status_filter)
    result = await kb.list_documents(page=page, page_size=page_size, status_filter=status_filter)
    return result.model_dump()


@app.get("/kb/pipeline/status")
async def kb_pipeline_status():
    kb = _require_kb()
    result = await kb.get_pipeline_status()
    return result.model_dump()


@app.get("/kb/status_counts")
async def kb_status_counts():
    kb = _require_kb()
    result = await kb.get_status_counts()
    return result.model_dump()


@app.post("/kb/query")
async def kb_query(body: dict[str, Any]):
    kb = _require_kb()
    query = str(body.get("query", "")).strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    top_k = int(body.get("top_k", 10))
    logger.info("API: KB query", event="api.kb.query", query=query, top_k=top_k)
    result = await kb.query(query=query, top_k=top_k)
    logger.info(
        "API: KB query done",
        event="api.kb.query.done",
        entities=len(result.entities),
        relationships=len(result.relationships),
        chunks=len(result.chunks),
    )
    return result.model_dump()


@app.post("/kb/vlm/parse")
async def kb_vlm_parse(file: UploadFile):
    """Parse a file using VLM and return markdown via SSE progress stream."""
    if vlm_client is None:
        raise HTTPException(status_code=404, detail="VLM parser is not configured")

    file_bytes = await file.read()
    filename = file.filename or "upload"
    logger.info("API: VLM parse requested", event="api.vlm.parse", filename=filename, size=len(file_bytes))

    async def stream():
        import json as _json
        try:
            job_id = await vlm_client.submit(file_bytes, filename)
            yield f"data: {_json.dumps({'state': 'submitted', 'job_id': job_id})}\n\n"

            result = await vlm_client.wait_for_result(
                job_id,
                on_progress=None,
            )
            combined = "\n\n".join(result.markdown_pages)
            logger.info("API: VLM parse completed", event="api.vlm.parse.done", filename=filename, pages=len(result.markdown_pages))
            yield f"data: {_json.dumps({'state': 'done', 'markdown': combined, 'page_count': len(result.markdown_pages)})}\n\n"
        except Exception as exc:
            logger.error("API: VLM parse failed", event="api.vlm.parse.error", filename=filename, error=str(exc))
            yield f"data: {_json.dumps({'state': 'failed', 'error': str(exc)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
