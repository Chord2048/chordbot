from __future__ import annotations

from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field


Role = Literal["user", "assistant", "tool"]

# Todo types
TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TodoPriority = Literal["high", "medium", "low"]


class TodoItem(BaseModel):
    """A single todo item in the session's task list."""
    id: str  # Unique identifier (UUID) for reconciliation
    content: str  # Task description in imperative form, e.g., "Run tests"
    status: TodoStatus = "pending"
    priority: TodoPriority = "medium"
    activeForm: str  # Present continuous form, e.g., "Running tests..."


class ModelRef(BaseModel):
    provider: str
    id: str


class PermissionRule(BaseModel):
    permission: str
    pattern: str
    action: Literal["allow", "deny", "ask"]


class Session(BaseModel):
    id: str
    title: str
    worktree: str
    cwd: str
    created_at: int
    updated_at: int
    permission_rules: list[PermissionRule]


class Message(BaseModel):
    id: str
    session_id: str
    role: Role
    parent_id: Optional[str] = None
    agent: str
    model: ModelRef
    created_at: int
    completed_at: Optional[int] = None
    finish: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    # Token tracking (for assistant messages)
    tokens: Optional[dict[str, int]] = None  # {"input": n, "output": n, "reasoning": n}
    cost: Optional[float] = None  # USD


class TextPart(BaseModel):
    id: str  # Part ID (ULID or UUID)
    message_id: str
    session_id: str
    type: Literal["text"] = "text"
    text: str
    synthetic: bool = False
    time: Optional[dict[str, int]] = None  # {"start": ts, "end": ts}


class ToolStatePending(BaseModel):
    status: Literal["pending"] = "pending"
    input: dict[str, Any] = Field(default_factory=dict)
    raw: str = ""


class ToolStateRunning(BaseModel):
    status: Literal["running"] = "running"
    input: dict[str, Any]
    title: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    time: dict[str, int]


class ToolStateCompleted(BaseModel):
    status: Literal["completed"] = "completed"
    input: dict[str, Any]
    title: str
    output: str
    metadata: dict[str, Any]
    time: dict[str, int]


class ToolStateError(BaseModel):
    status: Literal["error"] = "error"
    input: dict[str, Any]
    error: str
    metadata: Optional[dict[str, Any]] = None
    time: dict[str, int]


ToolState = Union[ToolStatePending, ToolStateRunning, ToolStateCompleted, ToolStateError]


class ToolPart(BaseModel):
    id: str  # Part ID
    message_id: str
    session_id: str
    type: Literal["tool"] = "tool"
    call_id: str
    tool: str
    state: ToolState


class ReasoningPart(BaseModel):
    id: str
    message_id: str
    session_id: str
    type: Literal["reasoning"] = "reasoning"
    text: str
    time: dict[str, int]  # {"start": ts, "end": ts}


Part = Union[TextPart, ToolPart, ReasoningPart]


class MessageWithParts(BaseModel):
    info: Message
    parts: list[Part]


class PermissionRequest(BaseModel):
    id: str
    session_id: str
    permission: str
    patterns: list[str]
    metadata: dict[str, Any]
    always: list[str]
    tool: Optional[dict[str, str]] = None


class PermissionReply(BaseModel):
    reply: Literal["once", "always", "reject"]
    message: Optional[str] = None


# --- API Request Models (for OpenAPI schema) ---

class CreateSessionRequest(BaseModel):
    worktree: str = Field(..., description="Absolute path to the worktree directory")
    title: str = Field(default="New session", description="Session title")
    cwd: str = Field(default="", description="Current working directory (defaults to worktree)")
    permission_rules: Optional[list[PermissionRule]] = Field(default=None, description="Permission rules (defaults to global config)")


class AddMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, description="User message text")


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1, description="New session title")

