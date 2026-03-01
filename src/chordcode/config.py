"""YAML/JSON file-based configuration with global + project-level merge.

Config discovery order (later wins):
  1. Built-in defaults (from config_schema.py)
  2. Global: ~/.chordcode/config.yaml (or .json)
  3. Project: {worktree}/.chordcode/config.yaml (or .json)
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from chordcode.config_schema import CONFIG_FIELD_META


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

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
class FeishuChannelConfig:
    enabled: bool
    app_id: str
    app_secret: str
    encrypt_key: str
    verification_token: str
    allow_from: list[str]
    permission_mode: Literal["deny", "allow", "commands"] = "deny"
    allowed_bash_commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChannelsConfig:
    feishu: FeishuChannelConfig


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
class LoggingConfig:
    level: str
    console: bool
    file: bool
    dir: str
    rotation: str
    retention: str


@dataclass(frozen=True)
class HooksConfig:
    debug: bool


@dataclass(frozen=True)
class WebSearchConfig:
    tavily_api_key: str


@dataclass(frozen=True)
class DaytonaConfig:
    api_key: str = ""
    server_url: str = ""
    target: str = ""
    default_workspace: str = "/workspace"


@dataclass(frozen=True)
class Config:
    openai: OpenAIConfig
    langfuse: LangfuseConfig
    channels: ChannelsConfig
    kb: KBConfig
    vlm: VLMConfig
    logging: LoggingConfig
    hooks: HooksConfig
    web_search: WebSearchConfig
    system_prompt: str
    db_path: str
    default_worktree: str
    default_permission_action: Literal["allow", "deny", "ask"]
    prompt_templates: dict[str, str]
    daytona: DaytonaConfig = field(default_factory=DaytonaConfig)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

GLOBAL_CONFIG_PATHS = (
    "~/.chordcode/config.yaml",
    "~/.chordcode/config.json",
)


def project_config_paths(worktree: str) -> tuple[str, ...]:
    if not worktree:
        return ()
    return (
        os.path.join(worktree, ".chordcode", "config.yaml"),
        os.path.join(worktree, ".chordcode", "config.json"),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _load_yaml_file(path: str) -> dict[str, Any] | None:
    """Load a YAML or JSON file defensively. Returns None on any error."""
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        if p.suffix == ".json":
            data = json.loads(raw)
        else:
            data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge; override wins for leaf values."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


# ---------------------------------------------------------------------------
# Defaults from schema
# ---------------------------------------------------------------------------

def _defaults_dict() -> dict[str, Any]:
    """Build a nested dict of defaults from CONFIG_FIELD_META."""
    d: dict[str, Any] = {}
    for key, meta in CONFIG_FIELD_META.items():
        parts = key.split(".")
        target = d
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = copy.deepcopy(meta.default)
    return d


# ---------------------------------------------------------------------------
# Build Config from merged dict
# ---------------------------------------------------------------------------

def _detect_worktree() -> str:
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists():
            return str(p)
    return str(cwd)


def _load_default_prompt() -> str:
    prompt_file = Path(__file__).parent / "prompts" / "default.txt"
    if prompt_file.exists():
        return prompt_file.read_text().strip()
    return "You are a helpful coding agent."


def _coerce_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off"):
            return False
    return default


def _coerce_str_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        items: list[str] = []
        text = v.replace("\r", "\n")
        for line in text.split("\n"):
            for token in line.split(","):
                s = token.strip()
                if s:
                    items.append(s)
        return items
    return []


def _get(d: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Get a value from a nested dict by dotted key."""
    parts = dotted.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def _build_config(merged: dict[str, Any]) -> Config:
    """Construct Config from a merged dict, applying defaults and coercion."""
    g = merged  # short alias

    openai = g.get("openai", {}) or {}
    base_url = str(openai.get("base_url", "") or "").strip()
    api_key = str(openai.get("api_key", "") or "").strip()
    model = str(openai.get("model", "") or "").strip()

    if not base_url:
        raise RuntimeError("openai.base_url is required in config")
    if not api_key:
        raise RuntimeError("openai.api_key is required in config")
    if not model:
        raise RuntimeError("openai.model is required in config")

    lf = g.get("langfuse", {}) or {}
    langfuse_sample_rate_raw = lf.get("sample_rate", 1.0)
    try:
        langfuse_sample_rate = float(langfuse_sample_rate_raw)
        if not 0.0 <= langfuse_sample_rate <= 1.0:
            langfuse_sample_rate = 1.0
    except (ValueError, TypeError):
        langfuse_sample_rate = 1.0

    system_prompt = str(g.get("system_prompt", "") or "").strip()
    if not system_prompt:
        system_prompt = _load_default_prompt()

    db_path = str(g.get("db_path", "./data/chordcode.sqlite3") or "./data/chordcode.sqlite3").strip()

    default_worktree = str(g.get("default_worktree", "") or "").strip()
    default_worktree = default_worktree if default_worktree else _detect_worktree()
    if not os.path.isabs(default_worktree):
        default_worktree = str(Path(default_worktree).resolve())

    dpa_raw = str(g.get("default_permission_action", "ask") or "ask").strip().lower()
    default_permission_action: Literal["allow", "deny", "ask"]
    if dpa_raw in ("allow", "deny", "ask"):
        default_permission_action = dpa_raw  # type: ignore[assignment]
    else:
        default_permission_action = "ask"

    log = g.get("logging", {}) or {}
    channels = g.get("channels", {}) or {}
    feishu = channels.get("feishu", {}) or {}
    feishu_permission_mode_raw = str(
        feishu.get("permission_mode", feishu.get("permissionMode", "deny")) or "deny"
    ).strip().lower()
    feishu_permission_mode: Literal["deny", "allow", "commands"]
    if feishu_permission_mode_raw in ("deny", "allow", "commands"):
        feishu_permission_mode = feishu_permission_mode_raw  # type: ignore[assignment]
    else:
        feishu_permission_mode = "deny"

    kb = g.get("kb", {}) or {}
    vlm = g.get("vlm", {}) or {}
    hooks = g.get("hooks", {}) or {}
    ws = g.get("web_search", {}) or {}
    daytona = g.get("daytona", {}) or {}
    pt = g.get("prompt_templates", {}) or {}
    if not isinstance(pt, dict):
        pt = {}

    return Config(
        openai=OpenAIConfig(base_url=base_url, api_key=api_key, model=model),
        langfuse=LangfuseConfig(
            enabled=_coerce_bool(lf.get("enabled", True), True),
            public_key=str(lf.get("public_key", "") or "").strip(),
            secret_key=str(lf.get("secret_key", "") or "").strip(),
            base_url=str(lf.get("base_url", "https://cloud.langfuse.com") or "https://cloud.langfuse.com").strip(),
            environment=str(lf.get("environment", "development") or "development").strip(),
            sample_rate=langfuse_sample_rate,
            debug=_coerce_bool(lf.get("debug", False), False),
        ),
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=_coerce_bool(feishu.get("enabled", False), False),
                app_id=str(feishu.get("app_id", feishu.get("appId", "")) or "").strip(),
                app_secret=str(feishu.get("app_secret", feishu.get("appSecret", "")) or "").strip(),
                encrypt_key=str(feishu.get("encrypt_key", feishu.get("encryptKey", "")) or "").strip(),
                verification_token=str(
                    feishu.get("verification_token", feishu.get("verificationToken", "")) or ""
                ).strip(),
                allow_from=_coerce_str_list(feishu.get("allow_from", feishu.get("allowFrom", []))),
                permission_mode=feishu_permission_mode,
                allowed_bash_commands=_coerce_str_list(
                    feishu.get("allowed_bash_commands", feishu.get("allowedBashCommands", []))
                ),
            )
        ),
        kb=KBConfig(
            backend=str(kb.get("backend", "lightrag") or "lightrag").strip(),
            base_url=str(kb.get("base_url", "") or "").strip(),
            api_key=str(kb.get("api_key", "") or "").strip(),
        ),
        vlm=VLMConfig(
            backend=str(vlm.get("backend", "none") or "none").strip(),
            api_url=str(vlm.get("api_url", "") or "").strip(),
            api_key=str(vlm.get("api_key", "") or "").strip(),
            poll_interval=int(vlm.get("poll_interval", 5) or 5),
            timeout=int(vlm.get("timeout", 1800) or 1800),
        ),
        logging=LoggingConfig(
            level=str(log.get("level", "INFO") or "INFO").strip().upper(),
            console=_coerce_bool(log.get("console", True), True),
            file=_coerce_bool(log.get("file", True), True),
            dir=str(log.get("dir", "./data/logs") or "./data/logs").strip(),
            rotation=str(log.get("rotation", "00:00") or "00:00").strip(),
            retention=str(log.get("retention", "7 days") or "7 days").strip(),
        ),
        hooks=HooksConfig(debug=_coerce_bool(hooks.get("debug", False), False)),
        web_search=WebSearchConfig(
            tavily_api_key=str(ws.get("tavily_api_key", "") or "").strip(),
        ),
        system_prompt=system_prompt,
        db_path=db_path,
        default_worktree=default_worktree,
        default_permission_action=default_permission_action,
        prompt_templates={str(k): str(v) for k, v in pt.items()},
        daytona=DaytonaConfig(
            api_key=str(daytona.get("api_key", "") or "").strip() or str(os.getenv("DAYTONA_API_KEY", "")).strip(),
            server_url=str(daytona.get("server_url", "") or "").strip() or str(os.getenv("DAYTONA_SERVER_URL", "")).strip(),
            target=str(daytona.get("target", "") or "").strip() or str(os.getenv("DAYTONA_TARGET", "")).strip(),
            default_workspace=str(daytona.get("default_workspace", "/workspace") or "/workspace").strip() or "/workspace",
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load(worktree_hint: str = "") -> Config:
    """Scan global + project config files, merge, and build Config."""
    merged = _defaults_dict()

    for path in GLOBAL_CONFIG_PATHS:
        data = _load_yaml_file(path)
        if data:
            merged = _deep_merge(merged, data)

    # Determine worktree: use hint, or whatever merged dict has, or auto-detect
    wt = worktree_hint or str(merged.get("default_worktree", "") or "").strip() or _detect_worktree()
    for path in project_config_paths(wt):
        data = _load_yaml_file(path)
        if data:
            merged = _deep_merge(merged, data)

    return _build_config(merged)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """Serialize Config back to a plain dict."""
    from dataclasses import asdict
    return asdict(cfg)


def mask_sensitive(d: dict[str, Any]) -> dict[str, Any]:
    """Replace sensitive values with '***' based on CONFIG_FIELD_META."""
    result = copy.deepcopy(d)
    for key, meta in CONFIG_FIELD_META.items():
        if not meta.sensitive:
            continue
        parts = key.split(".")
        target = result
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                break
            target = target[part]
        else:
            leaf = parts[-1]
            if isinstance(target, dict) and leaf in target and target[leaf]:
                target[leaf] = "***"
    return result


def save_config(data: dict[str, Any], path: str) -> None:
    """Write a dict as YAML to the given path, creating parent dirs."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")


def generate_default_yaml() -> str:
    """Generate a commented YAML string from CONFIG_FIELD_META."""
    lines: list[str] = ["# Chord Code configuration", "# See config.yaml.example for full reference", ""]
    current_section = ""
    for key, meta in CONFIG_FIELD_META.items():
        parts = key.split(".")
        if len(parts) > 1 and parts[0] != current_section:
            current_section = parts[0]
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"# --- {current_section} ---")

        desc = f"# {meta.description}"
        if meta.choices:
            desc += f" ({' | '.join(meta.choices)})"
        if meta.sensitive:
            desc += " [sensitive]"
        lines.append(desc)

        # Build the key path
        if len(parts) == 1:
            val = _format_yaml_value(meta.default)
            lines.append(f"{parts[0]}: {val}")
        elif len(parts) == 2:
            # We group under section headers
            val = _format_yaml_value(meta.default)
            lines.append(f"# {parts[0]}.{parts[1]}: {val}")
        else:
            val = _format_yaml_value(meta.default)
            lines.append(f"# {key}: {val}")
        lines.append("")

    return "\n".join(lines)


def _format_yaml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        if not v:
            return '""'
        return f'"{v}"'
    if isinstance(v, dict):
        return "{}"
    return str(v)


def get_config_sources(worktree: str = "") -> list[dict[str, Any]]:
    """List discovered config file paths with exists/loaded status."""
    wt = worktree or _detect_worktree()
    sources: list[dict[str, Any]] = []

    for path in GLOBAL_CONFIG_PATHS:
        p = Path(path).expanduser()
        data = _load_yaml_file(path)
        sources.append({
            "path": str(p),
            "scope": "global",
            "exists": p.is_file(),
            "loaded": data is not None,
        })

    for path in project_config_paths(wt):
        p = Path(path)
        data = _load_yaml_file(path)
        sources.append({
            "path": str(p),
            "scope": "project",
            "exists": p.is_file(),
            "loaded": data is not None,
        })

    return sources
