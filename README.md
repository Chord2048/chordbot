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

## Notes
- Do not commit `.env` (contains secrets).
- This project uses an OpenAI-compatible Chat Completions endpoint via env vars (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`).
- Set `TAVILY_API_KEY` to enable the `websearch` tool (Tavily-based web search).
- For fast local testing you can set `CHORDCODE_DEFAULT_PERMISSION_ACTION=allow` to bypass permission prompts (recommended to keep `ask` by default).
