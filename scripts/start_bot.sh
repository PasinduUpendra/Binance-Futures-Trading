#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Claude Quant — Start Trading System
# ---------------------------------------------------------------------------
# Starts the Claude Quant trading system in either Docker or local mode.
#
# Usage:
#   chmod +x scripts/start_bot.sh
#   ./scripts/start_bot.sh --docker        # Production (Docker Compose)
#   ./scripts/start_bot.sh --docker-dev    # Development (with volume mounts)
#   ./scripts/start_bot.sh --local         # Local Python process
#
# The script:
#   1. Validates .env exists and has required keys
#   2. Starts services (Docker containers or local processes)
#   3. Waits for health checks to pass
#   4. Reports status
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

# -----------------------------------------------------------------------
# Parse arguments
# -----------------------------------------------------------------------
MODE="${1:---docker}"

case "$MODE" in
    --docker|--docker-dev|--local)
        ;;
    -h|--help)
        echo "Usage: $0 [--docker|--docker-dev|--local]"
        echo ""
        echo "  --docker       Start via Docker Compose (production)"
        echo "  --docker-dev   Start via Docker Compose (dev overrides)"
        echo "  --local        Start as local Python process"
        exit 0
        ;;
    *)
        log_error "Unknown mode: $MODE"
        echo "Usage: $0 [--docker|--docker-dev|--local]"
        exit 1
        ;;
esac

echo "============================================"
echo "  Claude Quant — Start"
echo "============================================"
echo ""
log_info "Mode: ${MODE}"
log_info "Project root: ${PROJECT_ROOT}"

# -----------------------------------------------------------------------
# 1. Validate environment
# -----------------------------------------------------------------------
log_step "Validating environment..."

if [ ! -f "${PROJECT_ROOT}/.env" ]; then
    log_error ".env file not found. Run ./scripts/setup.sh first."
    exit 1
fi

# Source .env for validation
set -a
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/.env"
set +a

# Validate required keys
REQUIRED_VARS=("BINANCE_API_KEY" "BINANCE_API_SECRET")
MISSING=()

for var in "${REQUIRED_VARS[@]}"; do
    val="${!var:-}"
    if [ -z "$val" ] || [ "$val" = "your_api_key_here" ] || [ "$val" = "your_api_secret_here" ]; then
        MISSING+=("$var")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    log_error "Missing or unconfigured environment variables:"
    for var in "${MISSING[@]}"; do
        log_error "  - $var"
    done
    log_error "Edit .env with your actual API keys."
    exit 1
fi

# Check Anthropic key (warn but don't block)
if [ -z "${ANTHROPIC_API_KEY:-}" ] || [ "${ANTHROPIC_API_KEY:-}" = "your_anthropic_key_here" ]; then
    log_warn "ANTHROPIC_API_KEY not configured. Claude agent features will not work."
fi

TESTNET="${BINANCE_TESTNET:-true}"
if [ "$TESTNET" = "true" ]; then
    log_info "Running on Binance TESTNET (paper trading)"
else
    log_warn "Running on Binance PRODUCTION (real money!)"
    read -r -p "Are you sure you want to trade with real funds? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        log_info "Aborted. Set BINANCE_TESTNET=true in .env for paper trading."
        exit 0
    fi
fi

log_info "Environment validated."

# -----------------------------------------------------------------------
# 2. Start services
# -----------------------------------------------------------------------
case "$MODE" in
    --docker)
        log_step "Starting Docker Compose (production)..."

        cd "${PROJECT_ROOT}"

        # Build and start
        docker compose -f docker/docker-compose.yml build --quiet
        docker compose -f docker/docker-compose.yml up -d

        log_info "Containers starting..."

        # Wait for health checks
        log_step "Waiting for health checks..."
        MAX_WAIT=120
        WAITED=0
        INTERVAL=5

        while [ $WAITED -lt $MAX_WAIT ]; do
            # Check Freqtrade health
            FT_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' claude-quant-freqtrade 2>/dev/null || echo "starting")
            ORCH_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' claude-quant-orchestrator 2>/dev/null || echo "starting")

            if [ "$FT_HEALTH" = "healthy" ] && [ "$ORCH_HEALTH" = "healthy" ]; then
                log_info "All services healthy!"
                break
            fi

            echo -ne "\r  Freqtrade: ${FT_HEALTH}  |  Orchestrator: ${ORCH_HEALTH}  (${WAITED}s/${MAX_WAIT}s)  "
            sleep $INTERVAL
            WAITED=$((WAITED + INTERVAL))
        done
        echo ""

        if [ $WAITED -ge $MAX_WAIT ]; then
            log_warn "Some services may not be fully healthy yet. Check logs."
        fi

        # Show status
        echo ""
        docker compose -f docker/docker-compose.yml ps
        echo ""
        log_info "View logs: docker compose -f docker/docker-compose.yml logs -f"
        log_info "Stop:      ./scripts/stop_bot.sh --docker"
        ;;

    --docker-dev)
        log_step "Starting Docker Compose (development)..."

        cd "${PROJECT_ROOT}"

        docker compose \
            -f docker/docker-compose.yml \
            -f docker/docker-compose.dev.yml \
            build --quiet

        docker compose \
            -f docker/docker-compose.yml \
            -f docker/docker-compose.dev.yml \
            up -d

        log_info "Dev containers starting..."

        # Brief wait for startup
        log_step "Waiting for services..."
        sleep 10

        docker compose \
            -f docker/docker-compose.yml \
            -f docker/docker-compose.dev.yml \
            ps

        echo ""
        log_info "Freqtrade UI: http://localhost:8080"
        log_info "View logs: docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml logs -f"
        log_info "Stop:      ./scripts/stop_bot.sh --docker-dev"
        ;;

    --local)
        log_step "Starting locally..."

        # Check virtual environment
        if [ ! -d "${PROJECT_ROOT}/.venv" ]; then
            log_error "Virtual environment not found. Run ./scripts/setup.sh first."
            exit 1
        fi

        # Activate venv
        # shellcheck disable=SC1091
        source "${PROJECT_ROOT}/.venv/bin/activate"

        # Ensure state directories exist
        mkdir -p "${PROJECT_ROOT}/user_data/agent_state"
        mkdir -p "${PROJECT_ROOT}/user_data/logs"
        mkdir -p "${PROJECT_ROOT}/docs/reports"

        PID_FILE="${PROJECT_ROOT}/user_data/logs/orchestrator.pid"
        LOG_FILE="${PROJECT_ROOT}/user_data/logs/orchestrator.log"

        # Check if already running
        if [ -f "$PID_FILE" ]; then
            OLD_PID=$(cat "$PID_FILE")
            if kill -0 "$OLD_PID" 2>/dev/null; then
                log_error "Orchestrator already running (PID: $OLD_PID)"
                log_error "Stop it first: ./scripts/stop_bot.sh --local"
                exit 1
            else
                log_warn "Stale PID file found. Removing."
                rm -f "$PID_FILE"
            fi
        fi

        # Record start-of-day balance if not already set
        python -c "
import json, os
state_path = '${PROJECT_ROOT}/user_data/agent_state/daily_state.json'
try:
    with open(state_path, 'r') as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}
if 'start_of_day_balance' not in state:
    state['start_of_day_balance'] = float(os.getenv('INITIAL_CAPITAL', '75.0'))
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2)
    print('Initialised start-of-day balance.')
" 2>/dev/null || true

        # Start orchestrator in background
        log_info "Starting orchestrator..."
        cd "${PROJECT_ROOT}"

        PYTHONPATH="${PROJECT_ROOT}" \
        nohup python -m src.orchestrator.main \
            >> "$LOG_FILE" 2>&1 &

        ORCH_PID=$!
        echo "$ORCH_PID" > "$PID_FILE"
        disown "$ORCH_PID"  # Detach from shell — survives terminal close

        # Verify process started
        sleep 2
        if kill -0 "$ORCH_PID" 2>/dev/null; then
            log_info "Orchestrator started (PID: $ORCH_PID)"
        else
            log_error "Orchestrator failed to start. Check log:"
            log_error "  tail -50 $LOG_FILE"
            rm -f "$PID_FILE"
            exit 1
        fi

        echo ""
        log_info "Claude Quant is running locally."
        log_info "PID file: $PID_FILE"
        log_info "Log file: $LOG_FILE"
        log_info "Tail log: tail -f $LOG_FILE"
        log_info "Stop:     ./scripts/stop_bot.sh --local"
        ;;
esac

echo ""
echo -e "${GREEN}Claude Quant started successfully.${NC}"
