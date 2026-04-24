#!/usr/bin/env bash
set -uo pipefail
PROJECT_ROOT="/Users/pasinduupendra/Documents/Development/Claude Quant"
cd "$PROJECT_ROOT" || exit 1
mkdir -p user_data/logs

while true; do
  python3 scripts/heartbeat_monitor.py >> user_data/logs/heartbeat.stdout.log 2>> user_data/logs/heartbeat.stderr.log
  sleep 300
done
