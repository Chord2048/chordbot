"""TodoWrite tool for task planning and tracking."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from chordcode.bus.bus import Bus, Event
from chordcode.model import TodoItem
from chordcode.store.sqlite import SQLiteStore
from chordcode.tools.base import ToolResult


class TodoWriteTool:
    """
    Tool for creating and managing a structured task list.

    This tool enables the agent to:
    - Plan multi-step tasks visibly
    - Track progress in real-time
    - Demonstrate thoroughness to the user

    Constraints:
    - Maximum 20 todos per session
    - Only one todo can be in_progress at a time
    - Each call replaces the entire list (full replacement mode)
    - Use 'id' field for stable reconciliation across updates
    """

    name = "todowrite"
    description = """Use this tool to create and manage a structured task list for your current coding session. This helps you track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of the task and overall progress of their requests.

## When to Use This Tool
Use this tool proactively in these scenarios:

1. Complex multi-step tasks - When a task requires 3 or more distinct steps or actions
2. Non-trivial and complex tasks - Tasks that require careful planning or multiple operations
3. User explicitly requests todo list - When the user directly asks you to use the todo list
4. User provides multiple tasks - When users provide a list of things to be done (numbered or comma-separated)
5. After receiving new instructions - Immediately capture user requirements as todos
6. When you start working on a task - Mark it as in_progress BEFORE beginning work. Ideally you should only have one todo as in_progress at a time
7. After completing a task - Mark it as completed and add any new follow-up tasks discovered during implementation

## When NOT to Use This Tool

Skip using this tool when:
1. There is only a single, straightforward task
2. The task is trivial and tracking it provides no organizational benefit
3. The task can be completed in less than 3 trivial steps
4. The task is purely conversational or informational

NOTE that you should not use this tool if there is only one trivial task to do. In this case you are better off just doing the task directly.

## Task States and Management

1. **Task States**: Use these states to track progress:
   - pending: Task not yet started
   - in_progress: Currently working on (limit to ONE task at a time)
   - completed: Task finished successfully
   - cancelled: Task no longer needed

2. **Task Management**:
   - Update task status in real-time as you work
   - Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
   - Only have ONE task in_progress at any time
   - Complete current tasks before starting new ones
   - Cancel tasks that become irrelevant

3. **Task Breakdown**:
   - Create specific, actionable items
   - Break complex tasks into smaller, manageable steps
   - Use clear, descriptive task names
   - Always provide both content (imperative) and activeForm (present continuous)

When in doubt, use this tool. Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully."""

    MAX_TODOS = 20
    MAX_IN_PROGRESS = 1
    VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
    VALID_PRIORITIES = {"high", "medium", "low"}

    def __init__(self, store: SQLiteStore, bus: Bus) -> None:
        self._store = store
        self._bus = bus

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The updated todo list (replaces existing list)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Unique identifier for the todo item (use existing ID to update, or new UUID to create)",
                            },
                            "content": {
                                "type": "string",
                                "description": "Brief description of the task in imperative form (e.g., 'Run tests', 'Fix authentication bug')",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Current status of the task",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Priority level of the task",
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Present continuous form shown during execution (e.g., 'Running tests...', 'Fixing authentication bug...')",
                            },
                        },
                        "required": ["content", "status", "activeForm"],
                    },
                },
            },
            "required": ["todos"],
        }

    async def execute(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        """
        Execute the todowrite tool.

        Validates the input, applies constraints, stores the todos,
        and publishes an event for UI updates.
        """
        raw_todos = args.get("todos", [])

        if not isinstance(raw_todos, list):
            raise ValueError("todos must be an array")

        # Validate count
        if len(raw_todos) > self.MAX_TODOS:
            raise ValueError(f"Maximum {self.MAX_TODOS} todos allowed, got {len(raw_todos)}")

        # Validate and normalize each todo
        validated_todos: list[TodoItem] = []
        in_progress_count = 0

        for i, raw_todo in enumerate(raw_todos):
            if not isinstance(raw_todo, dict):
                raise ValueError(f"Todo {i}: must be an object")

            # Extract fields with defaults
            todo_id = str(raw_todo.get("id") or str(uuid4()))
            content = str(raw_todo.get("content", "")).strip()
            status = str(raw_todo.get("status", "pending")).lower()
            priority = str(raw_todo.get("priority", "medium")).lower()
            active_form = str(raw_todo.get("activeForm", "")).strip()

            # Validate required fields
            if not content:
                raise ValueError(f"Todo {i}: content is required")
            if not active_form:
                raise ValueError(f"Todo {i}: activeForm is required")

            # Validate enum values
            if status not in self.VALID_STATUSES:
                raise ValueError(f"Todo {i}: invalid status '{status}', must be one of {self.VALID_STATUSES}")
            if priority not in self.VALID_PRIORITIES:
                raise ValueError(f"Todo {i}: invalid priority '{priority}', must be one of {self.VALID_PRIORITIES}")

            # Count in_progress
            if status == "in_progress":
                in_progress_count += 1

            validated_todos.append(
                TodoItem(
                    id=todo_id,
                    content=content,
                    status=status,
                    priority=priority,
                    activeForm=active_form,
                )
            )

        # Validate single in_progress constraint
        if in_progress_count > self.MAX_IN_PROGRESS:
            raise ValueError(f"Only {self.MAX_IN_PROGRESS} task can be in_progress at a time, got {in_progress_count}")

        # Store the todos
        await self._store.update_todos(ctx.session_id, validated_todos)

        # Publish event for UI
        await self._bus.publish(
            Event(
                type="todo.updated",
                properties={
                    "session_id": ctx.session_id,
                    "todos": [t.model_dump() for t in validated_todos],
                },
            )
        )

        # Calculate stats for output
        pending_count = sum(1 for t in validated_todos if t.status == "pending")
        in_progress_count = sum(1 for t in validated_todos if t.status == "in_progress")
        completed_count = sum(1 for t in validated_todos if t.status == "completed")
        cancelled_count = sum(1 for t in validated_todos if t.status == "cancelled")

        # Build human-readable output
        output_lines = []
        for todo in validated_todos:
            status_icon = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
                "cancelled": "[-]",
            }[todo.status]

            line = f"{status_icon} {todo.content}"
            if todo.status == "in_progress":
                line += f" <- {todo.activeForm}"
            output_lines.append(line)

        output_lines.append("")
        output_lines.append(f"({completed_count}/{len(validated_todos)} completed)")

        return ToolResult(
            title=f"{len(validated_todos) - completed_count} pending todos",
            output="\n".join(output_lines),
            metadata={
                "todos": [t.model_dump() for t in validated_todos],
                "stats": {
                    "total": len(validated_todos),
                    "pending": pending_count,
                    "in_progress": in_progress_count,
                    "completed": completed_count,
                    "cancelled": cancelled_count,
                },
            },
        )
