from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, NotRequired, TypedDict

from chordcode.bus.bus import Event
from chordcode.config import Config


class Hook(StrEnum):
    All = "*"

    Config = "config"
    Event = "event"

    ChatMessage = "chat.message"
    ChatParams = "chat.params"
    ChatHeaders = "chat.headers"

    PermissionAsk = "permission.ask"

    ToolExecuteBefore = "tool.execute.before"
    ToolExecuteAfter = "tool.execute.after"

    McpServerConnect = "mcp.server.connect"
    McpToolCall = "mcp.tool.call"

    ExperimentalChatSystemTransform = "experimental.chat.system.transform"
    ExperimentalChatMessagesTransform = "experimental.chat.messages.transform"


ALL_HOOKS: tuple[Hook, ...] = (
    Hook.Config,
    Hook.Event,
    Hook.ChatMessage,
    Hook.ChatParams,
    Hook.ChatHeaders,
    Hook.PermissionAsk,
    Hook.ToolExecuteBefore,
    Hook.ToolExecuteAfter,
    Hook.McpServerConnect,
    Hook.McpToolCall,
    Hook.ExperimentalChatSystemTransform,
    Hook.ExperimentalChatMessagesTransform,
)


class ConfigInput(TypedDict):
    config: Config


class EventInput(TypedDict):
    event: Event


class ChatMessageInput(TypedDict):
    session_id: str
    agent: str
    message_id: str


class ChatMessageOutput(TypedDict):
    text: str


class ChatParamsInput(TypedDict):
    session_id: str
    agent: str
    model: dict[str, Any]
    message_id: str
    message: str


class ChatParamsOutput(TypedDict):
    temperature: float
    top_p: float
    top_k: int
    options: dict[str, Any]


class ChatHeadersInput(TypedDict):
    session_id: str
    agent: str
    model: dict[str, Any]
    message_id: str
    message: str


class ChatHeadersOutput(TypedDict):
    headers: dict[str, str]


class PermissionAskInput(TypedDict):
    session_id: str
    permission: str
    pattern: str
    patterns: list[str]
    metadata: dict[str, Any]
    always: list[str]
    tool: NotRequired[dict[str, str] | None]


class PermissionAskOutput(TypedDict):
    status: Literal["ask", "allow", "deny"]


class ToolExecuteBeforeInput(TypedDict):
    tool: str
    session_id: str
    call_id: str


class ToolExecuteBeforeOutput(TypedDict):
    args: dict[str, Any]


class ToolExecuteAfterInput(TypedDict):
    tool: str
    session_id: str
    call_id: str


class ToolExecuteAfterOutput(TypedDict):
    title: str
    output: str
    metadata: dict[str, Any]


class McpServerConnectInput(TypedDict):
    server: str
    type: str
    transport: str


class McpServerConnectOutput(TypedDict):
    pass


class McpToolCallInput(TypedDict):
    server: str
    tool: str
    args: dict[str, Any]


class McpToolCallOutput(TypedDict):
    pass


class ExperimentalChatSystemTransformInput(TypedDict):
    session_id: str
    agent: str
    model: dict[str, Any]


class ExperimentalChatSystemTransformOutput(TypedDict):
    system: list[str]


class ExperimentalChatMessagesTransformInput(TypedDict):
    session_id: str
    agent: str
    model: dict[str, Any]


class ExperimentalChatMessagesTransformOutput(TypedDict):
    messages: list[dict[str, Any]]

