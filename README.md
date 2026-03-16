# Chord Code (Agent Core, MVP)

Local-first agent core inspired by Open Code: agent loop, tool registry, event bus (SSE), SQLite persistence, and permission gates.

## Quickstart
Requirements:
- Python 3.11+
- `uv`

Setup:
```bash
cd chord-code
cp .env.example .env
uv sync
```

Run:
```bash
cd chord-code
uv run uvicorn chordcode.api.app:app --reload --port 4096
```

## Channel Integration (Feishu)
This project now includes a multi-channel adapter mechanism (extensible), with Feishu implemented in this iteration.

Example config:
```yaml
channels:
  feishu:
    enabled: true
    app_id: "cli_xxx"
    app_secret: "xxx"
    encrypt_key: ""
    verification_token: ""
    allow_from: []   # optional whitelist of open_id
```

Runtime status API:
```bash
curl http://127.0.0.1:4096/channels/status
```

## Runtime Backend (Local / Daytona)
Sessions now support `runtime.backend = local | daytona`.

Create Daytona session:
```bash
curl -X POST http://127.0.0.1:4096/sessions \
  -H "content-type: application/json" \
  -d '{
    "title": "Daytona Session",
    "worktree": "/workspace",
    "runtime": {
      "backend": "daytona",
      "daytona": {"sandbox_id": null}
    }
  }'
```

Configure Daytona in `config.yaml` (or via `DAYTONA_*` env vars):
```yaml
daytona:
  api_key: ""
  server_url: ""
  target: ""
  default_workspace: "/workspace"
```

## Cron Jobs
Agent can now be periodically awakened to run a task in an existing session.

API examples:
```bash
# Create a job (every 1 hour)
curl -X POST http://127.0.0.1:4096/cronjobs \
  -H "content-type: application/json" \
  -d '{
    "name": "hourly-summary",
    "session_id": "<session-id>",
    "message": "请总结最近进展并给出下一步计划",
    "schedule": {"kind": "every", "every_ms": 3600000}
  }'

# List jobs
curl http://127.0.0.1:4096/cronjobs
```

CLI examples:
```bash
chordcode cronjobs create --session-id <session-id> --name hourly-summary --message "请总结最近进展" --kind every --every-ms 3600000
chordcode cronjobs list
chordcode cronjobs runs <job-id>
```

## Local Memory
Chord Code now supports OpenClaw-style local memory for local sessions.

- Put long-term memory in `memory.md`
- Put dated memory archives in `memory/YYYY-MM-DD.md`
- Creating a new local session archives the previous local session into the current day's `memory/YYYY-MM-DD.md`
- The agent loads `memory.md` into prompt context for local sessions
- The agent can use `memory_search` and `memory_get` tools to query memory
- Detailed design and implementation notes: [docs/memory.md](docs/memory.md)

Example config:
```yaml
memory:
  enabled: true
  embedding_base_url: "https://api.openai.com/v1"
  embedding_api_key: "REPLACE_ME"
  embedding_model: "text-embedding-3-small"
  sync_interval_seconds: 3
```

## Tests
Run with pytest (recommended):
```bash
uv run pytest
```

Or with unittest:
```bash
uv run python -m unittest discover -s tests
```

## Logging
This project uses `loguru` and writes:
- Console: human-friendly, colored logs
- File: JSONL (one JSON object per line), rotated daily and retained for 7 days by default

Recommended usage in code:
```python
from chordcode.log import logger

logger.info("Session started", event="session.start", session_id="s1")
logger.error("Tool failed", event="tool.error", tool_name="bash", exc_info=err)

with logger.context(session_id="s1", message_id="m1", event="session.turn"):
    logger.debug("Running turn", turn=2)
```

The old `log.bind(...)`, `log_context(...)`, and `log_event(...)` APIs have been removed.

Env vars (all optional):
- `CHORDCODE_LOG_LEVEL` (default: `INFO`)
- `CHORDCODE_LOG_CONSOLE` (default: `true`)
- `CHORDCODE_LOG_FILE` (default: `true`)
- `CHORDCODE_LOG_DIR` (default: `./data/logs`)
- `CHORDCODE_LOG_ROTATION` (default: `00:00`)
- `CHORDCODE_LOG_RETENTION` (default: `7 days`)

Note: Uvicorn access logs are not included in the JSONL log file. If you want to disable access logs entirely, run Uvicorn with `--no-access-log`.

## Docs
- `chord-code/docs/project.md`
- `chord-code/docs/cronjobs.md`
- `chord-code/docs/memory.md`

## Notes
- Do not commit `.env` (contains secrets).
- This project uses an OpenAI-compatible Chat Completions endpoint via env vars (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`).
- Set `TAVILY_API_KEY` to enable the `websearch` tool (Tavily-based web search).
- For fast local testing you can set `CHORDCODE_DEFAULT_PERMISSION_ACTION=allow` to bypass permission prompts (recommended to keep `ask` by default).
