# CHANGES

## Unreleased

- Added cronjobs capability for scheduled agent wakeups and task execution.
- Added SQLite persistence for cron jobs and run history (`cron_jobs`, `cron_job_runs`).
- Added background `CronService` lifecycle integration on API startup/shutdown.
- Added cronjobs REST API (`/cronjobs`, `/cronjobs/{id}`, enable/disable, run-now, runs, status).
- Added CLI command group `chordcode cronjobs` for managing scheduled jobs.

## v0.2.0 — Configuration Migration (BREAKING)

**Breaking change**: All configuration has moved from `.env` / environment variables to **YAML config files**.

### What changed

- **Dropped `.env` and `python-dotenv`** — the app no longer reads environment variables for config.
- **New config files**: `~/.chordcode/config.yaml` (global) and `{worktree}/.chordcode/config.yaml` (project-level). Project overrides global via deep merge.
- **New dependency**: `pyyaml>=6.0` (replaces `python-dotenv>=1.0`).
- **New dataclasses**: `LoggingConfig`, `HooksConfig`, `WebSearchConfig` added to `Config`.
- **New field**: `Config.prompt_templates` — custom `{{variable}}` substitutions for system prompt (replaces `CHORDCODE_TPL_*` env vars).
- **Config schema registry**: `src/chordcode/config_schema.py` — flat metadata for all config fields (key, description, default, sensitive, choices).
- **Config API endpoints**:
  - `GET /config` — full merged config (sensitive fields masked)
  - `GET /config/schema` — field metadata for UI
  - `GET /config/sources` — config file discovery chain
  - `GET /config/raw?scope=project|global` — raw YAML content
  - `PUT /config/raw` — save raw YAML (validates syntax)
  - `PATCH /config` — partial update to project config
  - `POST /config/init` — generate default config file
- **Settings UI**: Full-page Settings panel in Web UI (gear icon in Activity Bar) with Visual (read-only field browser) and Raw YAML (editor with save) tabs.
- **Updated modules**:
  - `log.py`: `init_logging()` now accepts explicit keyword args instead of reading env vars.
  - `hooks.py`: `loghook()` accepts `cfg` param instead of reading `CHORDCODE_HOOK_DEBUG`.
  - `tools/web.py`: `TavilySearchTool` requires explicit `WebSearchCtx` (no env var fallback).
  - `prompts/template.py`: `render_prompt()` accepts `template_variables` param (replaces `CHORDCODE_TPL_*` env vars).

### Migration

1. Copy `config.yaml.example` to `~/.chordcode/config.yaml` (or `{worktree}/.chordcode/config.yaml`)
2. Move your `.env` values into the YAML structure (see `config.yaml.example` for mapping)
3. Delete `.env` (or leave it — it's ignored now)
4. Run `uv sync` to install `pyyaml` and remove `python-dotenv`
