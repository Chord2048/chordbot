# Chord Code CLI

Command-line interface for Chord Code. Requires a running server for most commands (except `serve` and `doctor` offline checks).

## Installation

```bash
uv sync
# Now available as: chordcode
# Or: uv run python -m chordcode
```

## Global Options

```
chordcode [--json] [--base-url URL] [--version] [--help]
```

- `--json` — Machine-readable JSON output (stdout=data, stderr=status)
- `--base-url` — Server URL (default: `http://127.0.0.1:4096`, env: `CHORDCODE_URL`)
- `--version` — Print version and exit

## Command Tree

```
chordcode
├── serve               Start the server (--daemon for background)
├── stop                Stop the daemon server
├── run MESSAGE         Quick-run: create session → send → run → stream
├── doctor              Validate setup and system health
├── config
│   ├── show            Merged config (sensitive masked)
│   ├── schema          Field metadata
│   ├── sources         Config file paths
│   ├── raw             Raw YAML (--scope project|global)
│   └── init            Generate default config
├── sessions
│   ├── list            (--limit --offset)
│   ├── get ID
│   ├── create          (--worktree --title --cwd)
│   ├── rename ID       (--title)
│   ├── delete ID
│   ├── messages ID
│   └── todos ID
├── agent
│   ├── send ID TEXT    Add user message
│   ├── run ID          Run agent loop
│   └── interrupt ID    Interrupt running session
├── cronjobs
│   ├── list            (--session-id --include-disabled/--enabled-only)
│   ├── get ID
│   ├── create          (--session-id --name --message --kind --at-ms --every-ms --expr --tz)
│   ├── delete ID
│   ├── enable ID
│   ├── disable ID
│   ├── run ID          (--force)
│   ├── runs ID         (--limit)
│   └── status
├── logs
│   ├── files           List log files
│   └── view            (--date --level --event --session-id --q --limit)
├── permissions
│   ├── pending         (--session-id)
│   └── reply ID        (--action --message)
├── skills
│   ├── list            (--worktree)
│   └── get NAME
├── mcp
│   ├── status
│   ├── tools
│   ├── connect NAME
│   ├── disconnect NAME
│   └── add             (--name --command|--url --args)
└── kb
    ├── config
    ├── status
    ├── counts
    ├── query TEXT       (--top-k)
    ├── documents        (--page --page-size)
    └── upload FILE      (--use-vlm)
```

## Examples

### Quick health check
```bash
chordcode doctor
chordcode doctor --json
```

### Start the server
```bash
chordcode serve --port 4096 --reload

# Run as a background daemon
chordcode serve --daemon
chordcode serve --daemon --port 8080

# Stop the daemon
chordcode stop
```

### Quick-run (end-to-end)
```bash
# Auto-approve permissions, stream output
chordcode run "Reply with PONG" --permission allow

# JSON mode, no streaming, auto-cleanup
chordcode run "List files in /tmp" --json --no-stream --permission allow

# Use existing session
chordcode run "Continue the task" --session-id abc-123
```

### Sessions
```bash
chordcode sessions list --limit 10
chordcode sessions create --worktree /path/to/project --title "My Session"
chordcode sessions messages <session-id>
chordcode sessions delete <session-id>
```

### Agent operations
```bash
chordcode agent send <session-id> "Explain the main module"
chordcode agent run <session-id>
chordcode agent interrupt <session-id>
```

### Cron Jobs
```bash
# Create an hourly wake-up task
chordcode cronjobs create \
  --session-id <session-id> \
  --name "hourly-summary" \
  --message "请总结最近进展并给出下一步计划" \
  --kind every \
  --every-ms 3600000

chordcode cronjobs list
chordcode cronjobs run <job-id> --force
chordcode cronjobs runs <job-id> --limit 20
chordcode cronjobs status
```

### Config inspection
```bash
chordcode config show --json
chordcode config sources
chordcode config raw --scope global
chordcode config init --scope project
```

### Logs
```bash
chordcode logs files
chordcode logs view --date 2025-01-15 --level ERROR --limit 20
```

### Permissions
```bash
chordcode permissions pending --session-id <id>
chordcode permissions reply <request-id> --action always
```

### Skills
```bash
chordcode skills list
chordcode skills get debug-chordcode
```

### MCP
```bash
chordcode mcp status
chordcode mcp tools
chordcode mcp connect my-server
chordcode mcp add --name local-fs --command npx --args "@modelcontextprotocol/server-filesystem,/tmp"
```

### Knowledge Base
```bash
chordcode kb config
chordcode kb counts
chordcode kb query "How does authentication work?" --top-k 5
chordcode kb upload ./docs/architecture.md
```

## JSON Mode

All commands support `--json` for machine-readable output:
- Data goes to **stdout** (JSON)
- Status/errors go to **stderr** (JSON)

```bash
# Pipe to jq
chordcode sessions list --json | jq '.[0].id'

# Use in scripts
SESSION_ID=$(chordcode sessions create --worktree /tmp --json | jq -r '.id')
```
