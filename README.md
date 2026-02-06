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

## Notes
- Do not commit `.env` (contains secrets).
- This project uses an OpenAI-compatible Chat Completions endpoint via env vars (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`).
- For fast local testing you can set `CHORDCODE_DEFAULT_PERMISSION_ACTION=allow` to bypass permission prompts (recommended to keep `ask` by default).
