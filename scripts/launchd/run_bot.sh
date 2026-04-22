#!/usr/bin/env bash
# Claude Quant launchd wrapper.
#
# launchd does not source the user's login shell, so this wrapper:
#   1. sets a minimal PATH that picks up Homebrew python and TA-Lib,
#   2. sources the repo's .env so BINANCE_* / ANTHROPIC_API_KEY /
#      SUPABASE_* are exported to the Python process,
#   3. execs the in-repo virtualenv's python running the orchestrator.
#
# All output is captured by launchd into the StandardOutPath /
# StandardErrorPath configured in com.claudequant.bot.plist.

set -euo pipefail

PROJECT_ROOT="/Users/pasinduupendra/Documents/Development/Claude Quant"
cd "$PROJECT_ROOT"

# macOS launchd hands us /usr/bin:/bin:/usr/sbin:/sbin only.
# Add Homebrew locations used by TA-Lib / python3.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

# Source .env if it exists. The `set -a` / `+a` dance exports every
# variable assigned while sourcing. chmod 600 on the .env file is the
# defence-in-depth recommended in docs/SPRINT1_IMPLEMENTATION.md §Secrets.
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Prefer the project venv (created via `python3 -m venv .venv`). Fall back
# to system python3 if the venv is absent — noisy, but keeps launchd alive
# so a human gets the log line instead of a silent failure.
if [[ -x "$PROJECT_ROOT/.venv/bin/python3" ]]; then
    PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
else
    echo "WARN: .venv not found — falling back to system python3" >&2
    PYTHON_BIN="$(command -v python3)"
fi

# Tell Python where to find the package + keep logs unbuffered so launchd
# flushes them immediately.
export PYTHONPATH="$PROJECT_ROOT"
export PYTHONUNBUFFERED=1

exec "$PYTHON_BIN" -m src.orchestrator.main
