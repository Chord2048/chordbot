---
name: debug-chordcode
description: Diagnostic commands and integration test scripts for verifying Chord Code health, inspecting config/sessions/logs, and running end-to-end tests via the CLI.
---

# Debug Chord Code

Use these commands to diagnose issues, verify system health, and run integration tests.

## Quick Health Check

```bash
chordcode doctor --json
```

## Config Inspection

```bash
chordcode config show --json        # Merged config (masked)
chordcode config sources            # Which files are loaded
chordcode config raw --scope global # Raw global YAML
```

## Log Query

```bash
chordcode logs files                                   # Available dates
chordcode logs view --date $(date +%Y-%m-%d) --level ERROR  # Today's errors
chordcode logs view --date $(date +%Y-%m-%d) --q "session_loop" --limit 20
```

## Session Lifecycle Test

```bash
# Create → Send → Run → Messages → Delete
SESSION=$(chordcode sessions create --worktree /tmp --title "test" --json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
chordcode agent send "$SESSION" "Reply PONG"
chordcode agent run "$SESSION"
chordcode sessions messages "$SESSION" --json
chordcode sessions delete "$SESSION"
```

## End-to-End Quick Test

```bash
chordcode run "Reply with exactly: PONG" --permission allow --json
```

## MCP / Skills / KB Status

```bash
chordcode mcp status --json
chordcode skills list --json
chordcode kb config --json
```

## Full Integration Test Script

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "=== Doctor ==="
chordcode doctor --json

echo "=== Config ==="
chordcode config show --json | python3 -c "import sys,json; d=json.load(sys.stdin); print('model:', d.get('openai',{}).get('model','?'))"

echo "=== Quick Run ==="
RESULT=$(chordcode run "Reply PONG" --permission allow --no-stream --json 2>/dev/null)
echo "Run result: $RESULT"

echo "=== Sessions ==="
chordcode sessions list --limit 3 --json

echo "=== Skills ==="
chordcode skills list --json

echo "=== MCP ==="
chordcode mcp status --json

echo "=== All checks passed ==="
```
