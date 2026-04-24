#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="/Users/pasinduupendra/Documents/Development/Claude Quant"
cd "$PROJECT_ROOT" || exit 1

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$PROJECT_ROOT"
export PYTHONUNBUFFERED=1

mkdir -p user_data/logs

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

while true; do
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] supervisor: starting bot" | tee -a user_data/logs/supervisor.log
  "$PROJECT_ROOT/.venv/bin/python3" -m src.orchestrator.main \
    >> user_data/logs/bot.stdout.log \
    2>> user_data/logs/bot.stderr.log

  code=$?
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] supervisor: bot exited with code=$code, restarting in 15s" | tee -a user_data/logs/supervisor.log
  sleep 15
done
