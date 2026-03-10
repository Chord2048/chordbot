"""Config field metadata registry.

Self-contained module (no project imports) that defines CONFIG_FIELD_META —
a flat registry of all config fields keyed by dotted path.
Used by config loading (for defaults) and the Settings UI (for descriptions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConfigFieldMeta:
    key: str
    description: str
    default: Any
    sensitive: bool = False
    choices: list[str] | None = None

    @property
    def type_name(self) -> str:
        if self.default is None:
            return "string"
        return type(self.default).__name__


CONFIG_FIELD_META: dict[str, ConfigFieldMeta] = {}


def _r(key: str, description: str, default: Any, *, sensitive: bool = False, choices: list[str] | None = None) -> None:
    CONFIG_FIELD_META[key] = ConfigFieldMeta(
        key=key, description=description, default=default,
        sensitive=sensitive, choices=choices,
    )


# --- OpenAI ---
_r("openai.base_url", "OpenAI-compatible base URL", "")
_r("openai.api_key", "API key for LLM provider", "", sensitive=True)
_r("openai.model", "Model identifier", "")

# --- Top-level ---
_r("system_prompt", "Global system prompt (empty = load from prompts/default.txt)", "")
_r("db_path", "SQLite database path", "./data/chordcode.sqlite3")
_r("default_worktree", "Default worktree (empty = auto-detect git root)", "")
_r("default_permission_action", "Default permission action for new sessions", "ask", choices=["ask", "allow", "deny"])

# --- Langfuse ---
_r("langfuse.enabled", "Enable Langfuse tracing", True)
_r("langfuse.public_key", "Langfuse public key", "", sensitive=True)
_r("langfuse.secret_key", "Langfuse secret key", "", sensitive=True)
_r("langfuse.base_url", "Langfuse server URL", "https://cloud.langfuse.com")
_r("langfuse.environment", "Langfuse tracing environment tag", "development")
_r("langfuse.sample_rate", "Trace sample rate (0.0 - 1.0)", 1.0)
_r("langfuse.debug", "Enable Langfuse debug logging", False)

# --- Channels ---
_r("channels.feishu.enabled", "Enable Feishu channel adapter", False)
_r("channels.feishu.app_id", "Feishu app id", "", sensitive=True)
_r("channels.feishu.app_secret", "Feishu app secret", "", sensitive=True)
_r("channels.feishu.encrypt_key", "Feishu event encrypt key (optional)", "", sensitive=True)
_r("channels.feishu.verification_token", "Feishu event verification token (optional)", "", sensitive=True)
_r("channels.feishu.allow_from", "Allowed Feishu sender open_ids (empty = allow all)", [])
_r(
    "channels.feishu.permission_mode",
    "Permission policy for channel-triggered tool calls",
    "deny",
    choices=["deny", "allow", "commands"],
)
_r(
    "channels.feishu.allowed_bash_commands",
    "Allowed bash command patterns when permission_mode=commands (fnmatch patterns)",
    [],
)

# --- Logging ---
_r("logging.level", "Log level", "INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
_r("logging.console", "Enable console log output", True)
_r("logging.file", "Enable file log output", True)
_r("logging.dir", "Log file directory", "./data/logs")
_r("logging.rotation", "Log rotation schedule (Loguru syntax)", "00:00")
_r("logging.retention", "Log retention period (Loguru syntax)", "7 days")

# --- Knowledge Base ---
_r("kb.backend", "KB backend type", "lightrag", choices=["lightrag", "none"])
_r("kb.base_url", "KB server URL (empty = disabled)", "")
_r("kb.api_key", "KB auth token", "", sensitive=True)

# --- VLM ---
_r("vlm.backend", "VLM parser backend", "none", choices=["paddleocr", "none"])
_r("vlm.api_url", "VLM API base URL", "")
_r("vlm.api_key", "VLM bearer token", "", sensitive=True)
_r("vlm.poll_interval", "VLM status poll interval (seconds)", 5)
_r("vlm.timeout", "VLM max wait time (seconds)", 1800)

# --- Hooks ---
_r("hooks.debug", "Enable hook debug logging", False)

# --- Web Search ---
_r("web_search.tavily_api_key", "Tavily API key for web search", "", sensitive=True)

# --- Memory ---
_r("memory.enabled", "Enable local memory indexing and retrieval", True)
_r("memory.embedding_base_url", "OpenAI-compatible embeddings base URL", "")
_r("memory.embedding_api_key", "API key for memory embeddings", "", sensitive=True)
_r("memory.embedding_model", "Embedding model identifier for memory search", "")
_r("memory.sync_interval_seconds", "Background memory sync interval in seconds", 3)

# --- Daytona Runtime ---
_r("daytona.api_key", "Daytona API key", "", sensitive=True)
_r("daytona.server_url", "Daytona server URL", "")
_r("daytona.target", "Daytona target environment", "")
_r("daytona.default_workspace", "Default workspace path for Daytona sessions", "/workspace")

# --- Prompt Templates ---
_r("prompt_templates", "Custom template variables for system prompt (dict)", {})
