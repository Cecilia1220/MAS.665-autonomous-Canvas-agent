#!/bin/bash
# Cron-safe wrapper for one Canvas agent cycle. Secrets stay outside this repository.

set -eu

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/cron.log"
SECRETS_FILE="$HOME/.canvas_agent_env"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

cd "$PROJECT_DIR"

if [ ! -r "$SECRETS_FILE" ]; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ERROR: secrets file is missing or unreadable: $SECRETS_FILE"
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ERROR: virtualenv Python is missing: $PYTHON_BIN"
  exit 1
fi

# The file contains only local export statements, for example CANVAS_TOKEN=... .
. "$SECRETS_FILE"

"$PYTHON_BIN" "$PROJECT_DIR/agent.py"
