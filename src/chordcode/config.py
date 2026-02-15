from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class OpenAIConfig:
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class LangfuseConfig:
    enabled: bool
    public_key: str
    secret_key: str
    base_url: str
    environment: str
    sample_rate: float
    debug: bool


@dataclass(frozen=True)
class KBConfig:
    backend: str           # "lightrag" | "none"
    base_url: str          # LightRAG server URL (empty = KB disabled)
    api_key: str           # Optional auth token


@dataclass(frozen=True)
class VLMConfig:
    backend: str           # "paddleocr" | "none"
    api_url: str           # PaddleOCR async API base URL
    api_key: str           # PaddleOCR bearer token
    poll_interval: int     # Seconds between status polls
    timeout: int           # Max wait seconds


@dataclass(frozen=True)
class Config:
    openai: OpenAIConfig
    langfuse: LangfuseConfig
    kb: KBConfig
    vlm: VLMConfig
    system_prompt: str
    db_path: str
    default_worktree: str
    default_permission_action: Literal["allow", "deny", "ask"]


def _detect_worktree() -> str:
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists():
            return str(p)
    return str(cwd)


def _load_default_prompt() -> str:
    """Load the default system prompt from the prompts directory."""
    prompt_file = Path(__file__).parent / "prompts" / "default.txt"
    if prompt_file.exists():
        return prompt_file.read_text().strip()
    return "You are a helpful coding agent."


def load() -> Config:
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()

    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL is required")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    if not model:
        raise RuntimeError("OPENAI_MODEL is required")

    # Langfuse configuration
    langfuse_enabled_str = os.environ.get("LANGFUSE_ENABLED", "true").strip().lower()
    langfuse_enabled = langfuse_enabled_str not in ("false", "0", "no")
    langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    langfuse_base_url = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").strip()
    langfuse_environment = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "development").strip()
    
    langfuse_sample_rate_str = os.environ.get("LANGFUSE_SAMPLE_RATE", "1.0").strip()
    try:
        langfuse_sample_rate = float(langfuse_sample_rate_str)
        if not 0.0 <= langfuse_sample_rate <= 1.0:
            langfuse_sample_rate = 1.0
    except ValueError:
        langfuse_sample_rate = 1.0
    
    langfuse_debug_str = os.environ.get("LANGFUSE_DEBUG", "false").strip().lower()
    langfuse_debug = langfuse_debug_str in ("true", "1", "yes")

    # System prompt: prefer env var, fall back to default.txt
    system_prompt = os.environ.get("CHORDCODE_SYSTEM_PROMPT", "").strip()
    if not system_prompt:
        system_prompt = _load_default_prompt()
    db_path = os.environ.get("CHORDCODE_DB_PATH", "./data/chordcode.sqlite3").strip()
    default_worktree = os.environ.get("CHORDCODE_DEFAULT_WORKTREE", "").strip()
    default_worktree = default_worktree if default_worktree else _detect_worktree()
    if not os.path.isabs(default_worktree):
        default_worktree = str(Path(default_worktree).resolve())

    default_permission_action_raw = os.environ.get("CHORDCODE_DEFAULT_PERMISSION_ACTION", "ask").strip().lower()
    default_permission_action: Literal["allow", "deny", "ask"]
    if default_permission_action_raw in ("allow", "deny", "ask"):
        default_permission_action = default_permission_action_raw  # type: ignore[assignment]
    else:
        default_permission_action = "ask"

    return Config(
        openai=OpenAIConfig(base_url=base_url, api_key=api_key, model=model),
        langfuse=LangfuseConfig(
            enabled=langfuse_enabled,
            public_key=langfuse_public_key,
            secret_key=langfuse_secret_key,
            base_url=langfuse_base_url,
            environment=langfuse_environment,
            sample_rate=langfuse_sample_rate,
            debug=langfuse_debug,
        ),
        kb=KBConfig(
            backend=os.environ.get("CHORDCODE_KB_BACKEND", "lightrag").strip(),
            base_url=os.environ.get("CHORDCODE_KB_BASE_URL", "").strip(),
            api_key=os.environ.get("CHORDCODE_KB_API_KEY", "").strip(),
        ),
        vlm=VLMConfig(
            backend=os.environ.get("CHORDCODE_KB_VLM_BACKEND", "none").strip(),
            api_url=os.environ.get("CHORDCODE_KB_VLM_API_URL", "").strip(),
            api_key=os.environ.get("CHORDCODE_KB_VLM_API_KEY", "").strip(),
            poll_interval=int(os.environ.get("CHORDCODE_KB_VLM_POLL_INTERVAL", "5").strip()),
            timeout=int(os.environ.get("CHORDCODE_KB_VLM_TIMEOUT", "1800").strip()),
        ),
        system_prompt=system_prompt,
        db_path=db_path,
        default_worktree=default_worktree,
        default_permission_action=default_permission_action,
    )
