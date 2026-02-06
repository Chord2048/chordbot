from __future__ import annotations

import json
import os
import time
import aiosqlite
from typing import Any, Optional
from pydantic import TypeAdapter

from chordcode.model import Message, MessageWithParts, Part, PermissionRequest, PermissionRule, Session, TodoItem


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
                  permission_rules_json TEXT NOT NULL
                )
                """,
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
            await db.commit()

    async def create_session(self, session: Session) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO sessions (id,title,worktree,cwd,created_at,updated_at,permission_rules_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    session.id,
                    session.title,
                    session.worktree,
                    session.cwd,
                    session.created_at,
                    session.updated_at,
                    json.dumps([r.model_dump() for r in session.permission_rules]),
                ),
            )
            await db.commit()

    async def get_session(self, session_id: str) -> Session:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT id,title,worktree,cwd,created_at,updated_at,permission_rules_json FROM sessions WHERE id=?",
                (session_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise KeyError(f"session not found: {session_id}")
            rules = [PermissionRule.model_validate(x) for x in json.loads(row[6])]
            return Session(
                id=row[0],
                title=row[1],
                worktree=row[2],
                cwd=row[3],
                created_at=row[4],
                updated_at=row[5],
                permission_rules=rules,
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
                SELECT id,title,worktree,cwd,created_at,updated_at,permission_rules_json
                FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = await cur.fetchall()
            return [
                Session(
                    id=row[0],
                    title=row[1],
                    worktree=row[2],
                    cwd=row[3],
                    created_at=row[4],
                    updated_at=row[5],
                    permission_rules=[PermissionRule.model_validate(x) for x in json.loads(row[6])],
                )
                for row in rows
            ]

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
