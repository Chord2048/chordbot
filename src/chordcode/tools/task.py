from __future__ import annotations

from html import escape
from typing import Any

from chordcode.tools.base import ToolResult


class TaskTool:
    name = "task"

    def __init__(self, *, service, parent_session) -> None:
        self._service = service
        self._parent_session = parent_session
        self.description = self._build_description()

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short task description for progress display."},
                "prompt": {"type": "string", "description": "Detailed instructions for the subagent."},
                "subagent_type": {"type": "string", "enum": ["explore"]},
                "session_id": {"type": "string", "description": "Optional existing subagent session to continue."},
            },
            "required": ["description", "prompt", "subagent_type"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        description = str(args.get("description", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        subagent_type = str(args.get("subagent_type", "")).strip()
        session_id = str(args.get("session_id", "")).strip() or None
        if not description:
            raise ValueError("description is required")
        if not prompt:
            raise ValueError("prompt is required")
        if not subagent_type:
            raise ValueError("subagent_type is required")
        return await self._service.execute_task(
            parent_session=self._parent_session,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            resume_session_id=session_id,
            parent_ctx=ctx,
        )

    def _build_description(self) -> str:
        agents = self._service.registry.list(mode="subagent")
        lines = [
            "Launch a subagent for a focused subtask.",
            "To run subagents in parallel, emit a single assistant response containing only multiple task calls.",
            "",
            "Available subagent types:",
        ]
        for agent in agents:
            lines.append(f"- {escape(agent.name)}: {escape(agent.description)}")
        return "\n".join(lines)
