#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Claude Quant — Graceful Shutdown Script
# ---------------------------------------------------------------------------
# Performs a clean shutdown of the Claude Quant trading system:
#   1. Sends SIGTERM for graceful shutdown
#   2. Waits for the orchestrator to handle open orders
#   3. Saves final state snapshot
#   4. Force-kills only as a last resort
#
# Usage:
#   chmod +x scripts/stop_bot.sh
#   ./scripts/stop_bot.sh --docker        # Stop Docker containers
#   ./scripts/stop_bot.sh --docker-dev    # Stop dev Docker containers
#   ./scripts/stop_bot.sh --local         # Stop local processes
# ---------------------------------------------------------------------------

set -euo pipefail

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}==>${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${1:---docker}"

echo "============================================"
echo "  Claude Quant — Shutdown"
echo "============================================"
echo ""
log_info "Mode: ${MODE}"

# -----------------------------------------------------------------------
# Helper: save a final state snapshot before shutting down
# -----------------------------------------------------------------------
save_final_state() {
    log_step "Saving final state snapshot..."

    python -c "
import json, os
from datetime import datetime, timezone

state_dir = '${PROJECT_ROOT}/user_data/agent_state'
os.makedirs(state_dir, exist_ok=True)

# Record shutdown timestamp in daily state
daily_path = os.path.join(state_dir, 'daily_state.json')
try:
    with open(daily_path, 'r') as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}

state['last_shutdown'] = datetime.now(timezone.utc).isoformat()
state['shutdown_clean'] = True

with open(daily_path, 'w') as f:
    json.dump(state, f, indent=2, default=str)

print('State snapshot saved.')
" 2>/dev/null || log_warn "Could not save final state (non-critical)."
}

# -----------------------------------------------------------------------
# Docker shutdown
# -----------------------------------------------------------------------
docker_shutdown() {
    local compose_files="$1"
    local grace_period="${2:-60}"

    log_step "Stopping Docker containers (grace period: ${grace_period}s)..."

    cd "${PROJECT_ROOT}"

    # Check if containers are running
    if ! docker compose $compose_files ps --quiet 2>/dev/null | grep -q .; then
        log_info "No running containers found."
        return 0
    fi

    # Show current status
    echo ""
    docker compose $compose_files ps
    echo ""

    # Graceful stop with timeout
    # The orchestrator container has stop_grace_period=60s in compose,
    # which lets it finish handling open orders before being killed.
    log_info "Sending stop signal to containers..."
    docker compose $compose_files stop --timeout "$grace_period"

    # Remove containers (but preserve volumes)
    docker compose $compose_files down --timeout 10

    log_info "All containers stopped and removed."

    # Show volume status
    echo ""
    log_info "Persistent volumes preserved:"
    docker volume ls --filter "name=claude-quant" --format "  - {{.Name}}" 2>/dev/null || true
}

# -----------------------------------------------------------------------
# Local shutdown
# -----------------------------------------------------------------------
local_shutdown() {
    PID_FILE="${PROJECT_ROOT}/user_data/logs/orchestrator.pid"
    LOG_FILE="${PROJECT_ROOT}/user_data/logs/orchestrator.log"
    GRACE_PERIOD=60

    if [ ! -f "$PID_FILE" ]; then
        log_warn "No PID file found at ${PID_FILE}."
        log_warn "Orchestrator may not be running."

        # Try to find the process anyway
        FOUND_PID=$(pgrep -f "src.orchestrator.main" 2>/dev/null || true)
        if [ -n "$FOUND_PID" ]; then
            log_warn "Found orchestrator process: PID $FOUND_PID"
            read -r -p "Kill it? [y/N] " response
            if [[ "$response" =~ ^[Yy]$ ]]; then
                echo "$FOUND_PID" > "$PID_FILE"
            else
                return 0
            fi
        else
            log_info "No orchestrator process found. Nothing to stop."
            return 0
        fi
    fi

    PID=$(cat "$PID_FILE")

    if ! kill -0 "$PID" 2>/dev/null; then
        log_warn "Process $PID is not running (stale PID file)."
        rm -f "$PID_FILE"
        return 0
    fi

    log_step "Stopping orchestrator (PID: $PID)..."

    # Phase 1: SIGTERM for graceful shutdown
    # The orchestrator should:
    #   - Stop opening new positions
    #   - Cancel pending (unfilled) orders
    #   - Update stop-losses on open positions
    #   - Save state to disk
    log_info "Phase 1: Sending SIGTERM for graceful shutdown..."
    kill -TERM "$PID"

    # Wait for graceful shutdown
    WAITED=0
    while [ $WAITED -lt $GRACE_PERIOD ]; do
        if ! kill -0 "$PID" 2>/dev/null; then
            log_info "Orchestrator shut down gracefully after ${WAITED}s."
            rm -f "$PID_FILE"
            return 0
        fi
        echo -ne "\r  Waiting for graceful shutdown... ${WAITED}s/${GRACE_PERIOD}s  "
        sleep 1
        WAITED=$((WAITED + 1))
    done
    echo ""

    # Phase 2: SIGINT as a stronger hint
    log_warn "Phase 2: SIGTERM timeout. Sending SIGINT..."
    kill -INT "$PID" 2>/dev/null || true

    # Wait 10 more seconds
    for _ in $(seq 1 10); do
        if ! kill -0 "$PID" 2>/dev/null; then
            log_info "Orchestrator stopped after SIGINT."
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done

    # Phase 3: SIGKILL as last resort
    log_warn "Phase 3: Process not responding. Force killing (SIGKILL)..."
    kill -9 "$PID" 2>/dev/null || true
    sleep 1

    if kill -0 "$PID" 2>/dev/null; then
        log_error "Failed to kill process $PID. Manual intervention required."
        return 1
    fi

    log_warn "Orchestrator force-killed. Some state may not have been saved."
    rm -f "$PID_FILE"
}

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
case "$MODE" in
    --docker)
        save_final_state
        docker_shutdown "-f docker/docker-compose.yml" 60
        ;;

    --docker-dev)
        save_final_state
        docker_shutdown "-f docker/docker-compose.yml -f docker/docker-compose.dev.yml" 30
        ;;

    --local)
        save_final_state
        local_shutdown
        ;;

    -h|--help)
        echo "Usage: $0 [--docker|--docker-dev|--local]"
        echo ""
        echo "  --docker       Stop Docker Compose production stack"
        echo "  --docker-dev   Stop Docker Compose development stack"
        echo "  --local        Stop local Python process"
        exit 0
        ;;

    *)
        log_error "Unknown mode: $MODE"
        echo "Usage: $0 [--docker|--docker-dev|--local]"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}Claude Quant stopped.${NC}"
