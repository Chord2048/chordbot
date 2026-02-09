from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from chordcode.bus.bus import Bus, Event
from chordcode.config import load
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooker, loghook
from chordcode.llm.openai_chat import OpenAIChatProvider
from chordcode.loop.interrupt import InterruptManager
from chordcode.loop.session_loop import SessionLoop
from chordcode.log import init_logging, log
from chordcode.model import Message, ModelRef, PermissionReply, PermissionRule, Session, TextPart
from chordcode.observability.langfuse_client import init_langfuse, flush_langfuse, shutdown_langfuse
from chordcode.observability.langfuse_hook import create_langfuse_hook
from chordcode.permission.service import PermissionService
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.bash import BashCtx, BashTool
from chordcode.tools.files import FileCtx, ReadTool, WriteTool
from chordcode.tools.skill import SkillCtx, SkillTool
from chordcode.tools.todo import TodoWriteTool
from chordcode.tools.registry import ToolRegistry

from dotenv import load_dotenv
load_dotenv(".env", override=True)

cfg = load()
init_logging()
log.bind(event="app.start", model=cfg.openai.model, openai_base_url=cfg.openai.base_url).info("Chord Code starting")
bus = Bus()
store = SQLiteStore(cfg.db_path)
hooks = Hooker()
lh = loghook()
if lh:
    hooks.add(lh)

# Initialize Langfuse tracing
init_langfuse(cfg.langfuse)

# Add Langfuse hook
langfuse_hook = create_langfuse_hook()
if langfuse_hook:
    hooks.add(langfuse_hook)

perm = PermissionService(bus, store, hooks)

llm = OpenAIChatProvider(
    base_url=cfg.openai.base_url,
    api_key=cfg.openai.api_key,
    model=cfg.openai.model,
    langfuse_enabled=cfg.langfuse.enabled,
)
interrupt = InterruptManager()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOG_FILE_RE = re.compile(r"^chordcode_(\d{4}-\d{2}-\d{2})\.jsonl$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _default_rules() -> list[PermissionRule]:
    return [
        PermissionRule(permission="*", pattern="*", action=cfg.default_permission_action),
    ]


app = FastAPI(title="Chord Code", version="0.1.0")

root = Path(__file__).resolve().parents[3]
web = root / "web"
app.mount("/static", StaticFiles(directory=str(web)), name="static")


@app.on_event("startup")
async def _startup():
    await store.init()
    await hooks.trigger(Hook.Config, {"config": cfg}, {})

    async def forward():
        async for e in bus.subscribe("*"):
            await hooks.trigger(Hook.Event, {"event": e}, {})

    asyncio.create_task(forward())


@app.on_event("shutdown")
async def _shutdown():
    """Shutdown handler to flush Langfuse events."""
    shutdown_langfuse()

@app.get("/")
async def home():
    return FileResponse(web / "index.html")

@app.get("/config")
async def get_config():
    return {"default_worktree": cfg.default_worktree}


def _resolve_log_dir() -> Path:
    raw = os.environ.get("CHORDCODE_LOG_DIR", "./data/logs").strip() or "./data/logs"
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
async def create_session(body: dict):
    worktree = str(body.get("worktree", "")).strip()
    if not worktree or not os.path.isabs(worktree):
        raise HTTPException(status_code=400, detail="worktree must be an absolute path")
    title = str(body.get("title") or "New session").strip()
    cwd = str(body.get("cwd") or worktree).strip()
    rules = body.get("permission_rules")
    permission_rules = [PermissionRule.model_validate(x) for x in rules] if rules else _default_rules()
    now = int(time.time() * 1000)
    s = Session(
        id=str(uuid4()),
        title=title,
        worktree=worktree,
        cwd=cwd,
        created_at=now,
        updated_at=now,
        permission_rules=permission_rules,
    )
    await store.create_session(s)
    await bus.publish(Event(type="session.created", properties={"session_id": s.id, "info": s.model_dump()}))
    return s


@app.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    """List all sessions, ordered by updated_at desc."""
    sessions = await store.list_sessions(limit=limit, offset=offset)
    return {"sessions": [s.model_dump() for s in sessions]}


@app.post("/sessions/{session_id}/messages")
async def add_message(session_id: str, body: dict):
    session = await store.get_session(session_id)
    text = str(body.get("text", ""))
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
    text_part = TextPart(
        id=str(uuid4()),
        message_id=msg.id,
        session_id=session.id,
        text=text,
    )
    await store.add_part(session.id, msg.id, text_part)
    await store.touch_session(session.id)
    await bus.publish(Event(type="message.updated", properties={"session_id": session.id, "info": msg.model_dump()}))
    await bus.publish(
        Event(
            type="message.part.updated",
            properties={"session_id": session.id, "message_id": msg.id, "part": text_part.model_dump(), "delta": text},
        ),
    )
    return {"message_id": msg.id}


@app.post("/sessions/{session_id}/run")
async def run_session(session_id: str):
    session = await store.get_session(session_id)
    tools = ToolRegistry(
        [
            BashTool(BashCtx(worktree=session.worktree, cwd=session.cwd)),
            ReadTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
            WriteTool(FileCtx(worktree=session.worktree, cwd=session.cwd)),
            SkillTool(SkillCtx(worktree=session.worktree, cwd=session.cwd, permission_rules=session.permission_rules)),
            TodoWriteTool(store=store, bus=bus),
        ],
    )
    loop = SessionLoop(cfg=cfg, bus=bus, store=store, perm=perm, tools=tools, llm=llm, interrupt=interrupt, hooks=hooks)
    msg_id, trace_id = await loop.run(session_id=session.id)
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


@app.post("/permissions/{request_id}/reply")
async def reply_permission(request_id: str, body: dict):
    reply = PermissionReply.model_validate(body)
    await perm.reply(request_id, reply)
    return {"ok": True}
