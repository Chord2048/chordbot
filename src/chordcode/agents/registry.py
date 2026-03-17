from __future__ import annotations

from pathlib import Path

from chordcode.agents.types import AgentDefinition, AgentLimits
from chordcode.model import PermissionRule


_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "agents"


class AgentRegistry:
    def __init__(self, agents: tuple[AgentDefinition, ...]) -> None:
        self._agents = {agent.name: agent for agent in agents}

    def get(self, name: str) -> AgentDefinition:
        agent = self._agents.get(name)
        if not agent:
            raise KeyError(f"unknown agent: {name}")
        return agent

    def list(self, *, mode: str | None = None) -> list[AgentDefinition]:
        agents = list(self._agents.values())
        if mode is None:
            return agents
        return [agent for agent in agents if agent.mode == mode]


agent_registry = AgentRegistry(
    (
        AgentDefinition(
            name="primary",
            mode="primary",
            description="Default primary agent.",
        ),
        AgentDefinition(
            name="explore",
            mode="subagent",
            description="Read-only code exploration agent.",
            prompt_template_path=str(_PROMPTS_DIR / "explore.txt"),
            tool_allowlist=frozenset({"read", "glob", "grep", "memory_search", "memory_get", "websearch", "webfetch"}),
            permission_profile=(
                PermissionRule(permission="external_directory", pattern="*", action="ask"),
                PermissionRule(permission="task", pattern="*", action="deny"),
                PermissionRule(permission="webfetch", pattern="*", action="allow"),
                PermissionRule(permission="websearch", pattern="*", action="allow"),
                PermissionRule(permission="memory_get", pattern="*", action="allow"),
                PermissionRule(permission="memory_search", pattern="*", action="allow"),
                PermissionRule(permission="grep", pattern="*", action="allow"),
                PermissionRule(permission="glob", pattern="*", action="allow"),
                PermissionRule(permission="read", pattern="*", action="allow"),
                PermissionRule(permission="*", pattern="*", action="deny"),
            ),
            limits=AgentLimits(max_turns=8, max_tool_calls=24, max_wall_time_ms=300_000),
        ),
    )
)
