from __future__ import annotations

import json
import os
import time
import aiosqlite
from typing import Any, Optional
from pydantic import TypeAdapter

from chordcode.model import (
    CronJob,
    CronJobRun,
    CronPayload,
    CronSchedule,
    CronJobState,
    Message,
    MessageWithParts,
    Part,
    PermissionRequest,
    PermissionRule,
    Session,
    TodoItem,
)


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self._path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  worktree TEXT NOT NULL,
                  cwd TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  permission_rules_json TEXT NOT NULL,
                  runtime_backend TEXT NOT NULL DEFAULT 'local',
                  runtime_json TEXT NOT NULL DEFAULT '{}'
                )
                """,
            )
            await self._migrate_sessions_runtime_columns(db)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_sessions (
                  channel TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  sender_id TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY (channel, chat_id)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_sessions_session_id ON channel_sessions(session_id)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  role TEXT NOT NULL,
                  parent_id TEXT,
                  agent TEXT NOT NULL,
                  model_provider TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  completed_at INTEGER,
                  finish TEXT,
                  error_json TEXT,
                  tool_call_id TEXT,
                  tool_name TEXT
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS parts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  message_id TEXT NOT NULL,
                  type TEXT NOT NULL,
                  content_json TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS permission_requests (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  permission TEXT NOT NULL,
                  patterns_json TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  always_json TEXT NOT NULL,
                  tool_json TEXT,
                  status TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  resolved_at INTEGER
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS permission_approvals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  permission TEXT NOT NULL,
                  pattern TEXT NOT NULL,
                  action TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                  id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  content TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  priority TEXT NOT NULL DEFAULT 'medium',
                  active_form TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY (session_id, id)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_todos_session ON todos(session_id)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_jobs (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  schedule_kind TEXT NOT NULL,
                  schedule_at_ms INTEGER,
                  schedule_every_ms INTEGER,
                  schedule_expr TEXT,
                  schedule_tz TEXT,
                  payload_kind TEXT NOT NULL,
                  payload_message TEXT NOT NULL,
                  next_run_at_ms INTEGER,
                  last_run_at_ms INTEGER,
                  last_status TEXT,
                  last_error TEXT,
                  last_assistant_message_id TEXT,
                  last_trace_id TEXT,
                  delete_after_run INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cron_jobs_next_run ON cron_jobs(enabled, next_run_at_ms)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_job_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  started_at INTEGER NOT NULL,
                  finished_at INTEGER,
                  status TEXT NOT NULL,
                  error TEXT,
                  assistant_message_id TEXT,
                  trace_id TEXT
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cron_job_runs_job ON cron_job_runs(job_id, started_at DESC)"
            )
            await db.commit()

    async def _migrate_sessions_runtime_columns(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(sessions)")
        rows = await cur.fetchall()
        cols = {str(r[1]) for r in rows}
        if "runtime_backend" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN runtime_backend TEXT NOT NULL DEFAULT 'local'")
        if "runtime_json" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'")

    async def create_session(self, session: Session) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO sessions (
                  id,title,worktree,cwd,created_at,updated_at,permission_rules_json,runtime_backend,runtime_json
                )
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    session.id,
                    session.title,
                    session.worktree,
                    session.cwd,
                    session.created_at,
                    session.updated_at,
                    json.dumps([r.model_dump() for r in session.permission_rules]),
                    session.runtime.backend,
                    json.dumps(session.runtime.model_dump()),
                ),
            )
            await db.commit()

    async def get_session(self, session_id: str) -> Session:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id,title,worktree,cwd,created_at,updated_at,permission_rules_json,runtime_backend,runtime_json
                FROM sessions WHERE id=?
                """,
                (session_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise KeyError(f"session not found: {session_id}")
            rules = [PermissionRule.model_validate(x) for x in json.loads(row[6])]
            runtime_json = {}
            try:
                runtime_json = json.loads(row[8]) if row[8] else {}
            except Exception:
                runtime_json = {}
            if not isinstance(runtime_json, dict):
                runtime_json = {}
            if "backend" not in runtime_json:
                runtime_json["backend"] = row[7] or "local"
            return Session(
                id=row[0],
                title=row[1],
                worktree=row[2],
                cwd=row[3],
                created_at=row[4],
                updated_at=row[5],
                permission_rules=rules,
                runtime=runtime_json,
            )

    async def touch_session(self, session_id: str) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            await db.commit()

    async def list_sessions(self, limit: int = 50, offset: int = 0) -> list[Session]:
        """List sessions ordered by updated_at desc."""
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id,title,worktree,cwd,created_at,updated_at,permission_rules_json,runtime_backend,runtime_json
                FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = await cur.fetchall()
            out: list[Session] = []
            for row in rows:
                runtime_json = {}
                try:
                    runtime_json = json.loads(row[8]) if row[8] else {}
                except Exception:
                    runtime_json = {}
                if not isinstance(runtime_json, dict):
                    runtime_json = {}
                if "backend" not in runtime_json:
                    runtime_json["backend"] = row[7] or "local"
                out.append(
                    Session(
                        id=row[0],
                        title=row[1],
                        worktree=row[2],
                        cwd=row[3],
                        created_at=row[4],
                        updated_at=row[5],
                        permission_rules=[PermissionRule.model_validate(x) for x in json.loads(row[6])],
                        runtime=runtime_json,
                    ),
                )
            return out

    async def update_session_title(self, session_id: str, title: str) -> Session:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, now, session_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"session not found: {session_id}")
            await db.commit()
        return await self.get_session(session_id)

    async def update_session_permission_rules(self, session_id: str, rules: list[PermissionRule]) -> Session:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "UPDATE sessions SET permission_rules_json=?, updated_at=? WHERE id=?",
                (json.dumps([r.model_dump() for r in rules]), now, session_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"session not found: {session_id}")
            await db.commit()
        return await self.get_session(session_id)

    async def update_session_runtime(self, session_id: str, runtime_backend: str, runtime_json: dict[str, Any]) -> Session:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "UPDATE sessions SET runtime_backend=?, runtime_json=?, updated_at=? WHERE id=?",
                (runtime_backend, json.dumps(runtime_json), now, session_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"session not found: {session_id}")
            await db.commit()
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,))
            if not await cur.fetchone():
                raise KeyError(f"session not found: {session_id}")

            # Cascade delete related rows owned by the session.
            await db.execute("DELETE FROM parts WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM channel_sessions WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM permission_requests WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM permission_approvals WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM todos WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM cron_job_runs WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM cron_jobs WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            await db.commit()

    async def get_channel_session(self, channel: str, chat_id: str) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT session_id FROM channel_sessions WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            )
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def bind_channel_session(
        self,
        *,
        channel: str,
        chat_id: str,
        session_id: str,
        sender_id: str | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO channel_sessions(channel, chat_id, session_id, sender_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, chat_id)
                DO UPDATE SET
                  session_id=excluded.session_id,
                  sender_id=excluded.sender_id,
                  updated_at=excluded.updated_at
                """,
                (channel, chat_id, session_id, sender_id, now, now),
            )
            await db.commit()

    async def add_message(self, message: Message) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO messages (
                  id, session_id, role, parent_id, agent, model_provider, model_id,
                  created_at, completed_at, finish, error_json, tool_call_id, tool_name
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message.id,
                    message.session_id,
                    message.role,
                    message.parent_id,
                    message.agent,
                    message.model.provider,
                    message.model.id,
                    message.created_at,
                    message.completed_at,
                    message.finish,
                    json.dumps(message.error) if message.error else None,
                    message.tool_call_id,
                    message.tool_name,
                ),
            )
            await db.commit()

    async def update_message(
        self,
        message_id: str,
        *,
        completed_at: Optional[int] = None,
        finish: Optional[str] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE messages
                SET
                  completed_at=COALESCE(?,completed_at),
                  finish=COALESCE(?,finish),
                  error_json=COALESCE(?,error_json)
                WHERE id=?
                """,
                (completed_at, finish, json.dumps(error) if error else None, message_id),
            )
            await db.commit()

    async def add_part(self, session_id: str, message_id: str, part: Part) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO parts (session_id,message_id,type,content_json,created_at) VALUES (?,?,?,?,?)",
                (
                    session_id,
                    message_id,
                    part.type,
                    json.dumps(part.model_dump()),
                    int(time.time() * 1000),
                ),
            )
            await db.commit()

    async def list_messages(self, session_id: str) -> list[MessageWithParts]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id,role,parent_id,agent,model_provider,model_id,created_at,completed_at,finish,error_json,tool_call_id,tool_name
                FROM messages WHERE session_id=? ORDER BY created_at ASC
                """,
                (session_id,),
            )
            msg_rows = await cur.fetchall()
            out: list[MessageWithParts] = []
            for r in msg_rows:
                info = Message(
                    id=r[0],
                    session_id=session_id,
                    role=r[1],
                    parent_id=r[2],
                    agent=r[3],
                    model={"provider": r[4], "id": r[5]},
                    created_at=r[6],
                    completed_at=r[7],
                    finish=r[8],
                    error=json.loads(r[9]) if r[9] else None,
                    tool_call_id=r[10],
                    tool_name=r[11],
                )
                parts_cur = await db.execute(
                    "SELECT content_json FROM parts WHERE session_id=? AND message_id=? ORDER BY id ASC",
                    (session_id, info.id),
                )
                part_rows = await parts_cur.fetchall()
                part_adapter = TypeAdapter(Part)
                parts = [part_adapter.validate_python(json.loads(p[0])) for p in part_rows]
                out.append(MessageWithParts(info=info, parts=parts))
            return out

    async def create_permission_request(self, req: PermissionRequest) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO permission_requests (
                  id,session_id,permission,patterns_json,metadata_json,always_json,tool_json,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    req.id,
                    req.session_id,
                    req.permission,
                    json.dumps(req.patterns),
                    json.dumps(req.metadata),
                    json.dumps(req.always),
                    json.dumps(req.tool) if req.tool else None,
                    "pending",
                    int(time.time() * 1000),
                ),
            )
            await db.commit()

    async def resolve_permission_request(self, request_id: str, status: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE permission_requests SET status=?, resolved_at=? WHERE id=?",
                (status, int(time.time() * 1000), request_id),
            )
            await db.commit()

    async def add_approval(self, session_id: str, permission: str, pattern: str, action: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO permission_approvals (session_id,permission,pattern,action) VALUES (?,?,?,?)",
                (session_id, permission, pattern, action),
            )
            await db.commit()

    async def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT permission,pattern,action FROM permission_approvals WHERE session_id=?",
                (session_id,),
            )
            rows = await cur.fetchall()
            return [{"permission": r[0], "pattern": r[1], "action": r[2]} for r in rows]

    async def list_pending_permission_requests(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id,permission,patterns_json,metadata_json,always_json,tool_json,created_at
                FROM permission_requests
                WHERE session_id=? AND status='pending'
                ORDER BY created_at ASC
                """,
                (session_id,),
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "session_id": session_id,
                    "permission": r[1],
                    "patterns": json.loads(r[2]),
                    "metadata": json.loads(r[3]),
                    "always": json.loads(r[4]),
                    "tool": json.loads(r[5]) if r[5] else None,
                    "created_at": r[6],
                }
                for r in rows
            ]

    # ----- Todo operations -----

    async def get_todos(self, session_id: str) -> list[TodoItem]:
        """Get all todos for a session, ordered by position."""
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id, content, status, priority, active_form
                FROM todos
                WHERE session_id = ?
                ORDER BY position ASC
                """,
                (session_id,),
            )
            rows = await cur.fetchall()
            return [
                TodoItem(
                    id=r[0],
                    content=r[1],
                    status=r[2],
                    priority=r[3],
                    activeForm=r[4],
                )
                for r in rows
            ]

    async def update_todos(self, session_id: str, todos: list[TodoItem]) -> None:
        """
        Replace the entire todo list for a session.

        This is an atomic operation:
        1. Delete all existing todos for the session
        2. Insert the new list with proper positions
        """
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            # Delete existing todos
            await db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))

            # Insert new todos with position
            for position, todo in enumerate(todos):
                await db.execute(
                    """
                    INSERT INTO todos (id, session_id, content, status, priority, active_form, position, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        todo.id,
                        session_id,
                        todo.content,
                        todo.status,
                        todo.priority,
                        todo.activeForm,
                        position,
                        now,
                        now,
                    ),
                )
            await db.commit()

    # ----- Cron job operations -----

    @staticmethod
    def _row_to_cron_job(row: tuple[Any, ...]) -> CronJob:
        return CronJob(
            id=row[0],
            name=row[1],
            session_id=row[2],
            enabled=bool(row[3]),
            schedule=CronSchedule(
                kind=row[4],
                at_ms=row[5],
                every_ms=row[6],
                expr=row[7],
                tz=row[8],
            ),
            payload=CronPayload(kind=row[9], message=row[10]),
            state=CronJobState(
                next_run_at_ms=row[11],
                last_run_at_ms=row[12],
                last_status=row[13],
                last_error=row[14],
                last_assistant_message_id=row[15],
                last_trace_id=row[16],
            ),
            delete_after_run=bool(row[17]),
            created_at_ms=row[18],
            updated_at_ms=row[19],
        )

    async def create_cron_job(self, job: CronJob) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO cron_jobs (
                  id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                  payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
                  last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.name,
                    job.session_id,
                    int(job.enabled),
                    job.schedule.kind,
                    job.schedule.at_ms,
                    job.schedule.every_ms,
                    job.schedule.expr,
                    job.schedule.tz,
                    job.payload.kind,
                    job.payload.message,
                    job.state.next_run_at_ms,
                    job.state.last_run_at_ms,
                    job.state.last_status,
                    job.state.last_error,
                    job.state.last_assistant_message_id,
                    job.state.last_trace_id,
                    int(job.delete_after_run),
                    job.created_at_ms,
                    job.updated_at_ms,
                ),
            )
            await db.commit()

    async def get_cron_job(self, job_id: str) -> CronJob:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT
                  id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                  payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
                  last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
                FROM cron_jobs
                WHERE id=?
                """,
                (job_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise KeyError(f"cron job not found: {job_id}")
            return self._row_to_cron_job(row)

    async def list_cron_jobs(self, *, session_id: str | None = None, include_disabled: bool = True) -> list[CronJob]:
        query = """
            SELECT
              id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
              payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
              last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
            FROM cron_jobs
        """
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if not include_disabled:
            clauses.append("enabled=1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(next_run_at_ms, 9223372036854775807) ASC, created_at ASC"

        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(query, tuple(params))
            rows = await cur.fetchall()
            return [self._row_to_cron_job(row) for row in rows]

    async def list_due_cron_jobs(self, now_ms: int, limit: int = 16) -> list[CronJob]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT
                  id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                  payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
                  last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
                FROM cron_jobs
                WHERE enabled=1 AND next_run_at_ms IS NOT NULL AND next_run_at_ms<=?
                ORDER BY next_run_at_ms ASC
                LIMIT ?
                """,
                (now_ms, max(1, limit)),
            )
            rows = await cur.fetchall()
            return [self._row_to_cron_job(row) for row in rows]

    async def get_next_cron_wake_ms(self) -> int | None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT MIN(next_run_at_ms) FROM cron_jobs WHERE enabled=1 AND next_run_at_ms IS NOT NULL"
            )
            row = await cur.fetchone()
            if not row or row[0] is None:
                return None
            return int(row[0])

    async def update_cron_job_enabled(self, job_id: str, *, enabled: bool, next_run_at_ms: int | None) -> CronJob:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                UPDATE cron_jobs
                SET enabled=?, next_run_at_ms=?, updated_at=?
                WHERE id=?
                """,
                (int(enabled), next_run_at_ms, now, job_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"cron job not found: {job_id}")
            await db.commit()
        return await self.get_cron_job(job_id)

    async def delete_cron_job(self, job_id: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
            await db.commit()
            return cur.rowcount > 0

    async def update_cron_job_runtime(
        self,
        job_id: str,
        *,
        next_run_at_ms: int | None,
        last_run_at_ms: int | None,
        last_status: str | None,
        last_error: str | None,
        last_assistant_message_id: str | None,
        last_trace_id: str | None,
        enabled: bool | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            if enabled is None:
                await db.execute(
                    """
                    UPDATE cron_jobs
                    SET next_run_at_ms=?, last_run_at_ms=?, last_status=?, last_error=?, last_assistant_message_id=?, last_trace_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        next_run_at_ms,
                        last_run_at_ms,
                        last_status,
                        last_error,
                        last_assistant_message_id,
                        last_trace_id,
                        now,
                        job_id,
                    ),
                )
            else:
                await db.execute(
                    """
                    UPDATE cron_jobs
                    SET enabled=?, next_run_at_ms=?, last_run_at_ms=?, last_status=?, last_error=?, last_assistant_message_id=?, last_trace_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        int(enabled),
                        next_run_at_ms,
                        last_run_at_ms,
                        last_status,
                        last_error,
                        last_assistant_message_id,
                        last_trace_id,
                        now,
                        job_id,
                    ),
                )
            await db.commit()

    async def create_cron_job_run(self, *, job_id: str, session_id: str, started_at_ms: int) -> int:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                INSERT INTO cron_job_runs (job_id, session_id, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (job_id, session_id, started_at_ms),
            )
            await db.commit()
            run_id = cur.lastrowid
            if run_id is None:
                raise RuntimeError("failed to create cron job run")
            return int(run_id)

    async def finish_cron_job_run(
        self,
        run_id: int,
        *,
        status: str,
        finished_at_ms: int,
        error: str | None = None,
        assistant_message_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE cron_job_runs
                SET status=?, finished_at=?, error=?, assistant_message_id=?, trace_id=?
                WHERE id=?
                """,
                (status, finished_at_ms, error, assistant_message_id, trace_id, run_id),
            )
            await db.commit()

    async def list_cron_job_runs(self, job_id: str, limit: int = 50) -> list[CronJobRun]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id, job_id, session_id, started_at, finished_at, status, error, assistant_message_id, trace_id
                FROM cron_job_runs
                WHERE job_id=?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (job_id, max(1, limit)),
            )
            rows = await cur.fetchall()
            return [
                CronJobRun(
                    id=row[0],
                    job_id=row[1],
                    session_id=row[2],
                    started_at_ms=row[3],
                    finished_at_ms=row[4],
                    status=row[5],
                    error=row[6],
                    assistant_message_id=row[7],
                    trace_id=row[8],
                )
                for row in rows
            ]
