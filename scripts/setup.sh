#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Claude Quant — Setup Script
# ---------------------------------------------------------------------------
# This script:
#   1. Checks Python version (3.11+)
#   2. Creates a virtual environment
#   3. Installs TA-Lib C library (platform-specific)
#   4. Installs Python dependencies
#   5. Copies .env.example to .env (if needed)
#   6. Initialises SQLite databases
#   7. Creates required directory structure and state files
#   8. Verifies exchange connectivity with a data fetch test
#
# Usage:
#   chmod +x scripts/setup.sh
#   ./scripts/setup.sh
# ---------------------------------------------------------------------------

set -euo pipefail

# Colours for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}==>${NC} $1"; }

# Resolve project root (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

echo "============================================"
echo "  Claude Quant — Setup"
echo "============================================"
echo ""
log_info "Project root: ${PROJECT_ROOT}"

# -----------------------------------------------------------------------
# 1. Check Python version
# -----------------------------------------------------------------------
log_step "Checking Python version..."

PYTHON_CMD=""
for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    log_error "Python 3.11+ is required but not found."
    log_error "Install Python 3.11+ and try again."
    exit 1
fi

log_info "Found Python: $($PYTHON_CMD --version) at $(which $PYTHON_CMD)"

# -----------------------------------------------------------------------
# 2. Create virtual environment
# -----------------------------------------------------------------------
log_step "Creating virtual environment..."

VENV_DIR="${PROJECT_ROOT}/.venv"

if [ -d "${VENV_DIR}" ]; then
    log_warn "Virtual environment already exists at ${VENV_DIR}"
    read -r -p "Recreate it? [y/N] " response
    if [[ "$response" =~ ^[Yy]$ ]]; then
        rm -rf "${VENV_DIR}"
        $PYTHON_CMD -m venv "${VENV_DIR}"
        log_info "Virtual environment recreated."
    else
        log_info "Keeping existing virtual environment."
    fi
else
    $PYTHON_CMD -m venv "${VENV_DIR}"
    log_info "Virtual environment created at ${VENV_DIR}"
fi

# Activate
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
log_info "Virtual environment activated."

# Upgrade pip
pip install --quiet --upgrade pip setuptools wheel

# -----------------------------------------------------------------------
# 3. Install TA-Lib C library
# -----------------------------------------------------------------------
log_step "Checking TA-Lib C library..."

TALIB_INSTALLED=false

# Check if TA-Lib is already installed
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if ldconfig -p 2>/dev/null | grep -q libta_lib; then
        TALIB_INSTALLED=true
        log_info "TA-Lib C library already installed (Linux)."
    fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
    if [ -f /usr/local/lib/libta_lib.dylib ] || [ -f /opt/homebrew/lib/libta_lib.dylib ]; then
        TALIB_INSTALLED=true
        log_info "TA-Lib C library already installed (macOS)."
    fi
fi

if [ "$TALIB_INSTALLED" = false ]; then
    log_info "Installing TA-Lib C library..."

    case "$OSTYPE" in
        darwin*)
            # macOS — use Homebrew
            if command -v brew &>/dev/null; then
                brew install ta-lib
                log_info "TA-Lib installed via Homebrew."
            else
                log_error "Homebrew not found. Install it from https://brew.sh/ then run:"
                log_error "  brew install ta-lib"
                exit 1
            fi
            ;;
        linux-gnu*)
            # Linux — build from source
            log_info "Building TA-Lib from source..."
            TALIB_VERSION="0.4.0"
            TALIB_URL="https://prdownloads.sourceforge.net/ta-lib/ta-lib-${TALIB_VERSION}-src.tar.gz"

            TMPDIR=$(mktemp -d)
            cd "$TMPDIR"
            wget -q "$TALIB_URL" -O ta-lib-src.tar.gz
            tar xzf ta-lib-src.tar.gz
            cd ta-lib

            ./configure --prefix=/usr/local
            make -j"$(nproc)"
            sudo make install
            sudo ldconfig

            cd "${PROJECT_ROOT}"
            rm -rf "$TMPDIR"
            log_info "TA-Lib C library built and installed."
            ;;
        *)
            log_error "Unsupported OS: $OSTYPE. Please install TA-Lib manually."
            log_error "See: https://github.com/TA-Lib/ta-lib-python#dependencies"
            exit 1
            ;;
    esac
fi

# -----------------------------------------------------------------------
# 4. Install Python dependencies
# -----------------------------------------------------------------------
log_step "Installing Python dependencies..."

pip install --quiet -r "${PROJECT_ROOT}/requirements.txt"
log_info "Core dependencies installed."

# Install dev dependencies
pip install --quiet -e "${PROJECT_ROOT}[dev]" 2>/dev/null || {
    log_warn "Editable install failed, installing dev deps directly..."
    pip install --quiet pytest pytest-asyncio pytest-cov pytest-mock mypy ruff
}
log_info "Dev dependencies installed."

# Verify TA-Lib Python binding
if python -c "import talib; print(f'TA-Lib Python version: {talib.__version__}')" 2>/dev/null; then
    log_info "TA-Lib Python binding verified."
else
    log_warn "TA-Lib Python binding failed to import. Attempting reinstall..."
    pip install --no-cache-dir --force-reinstall TA-Lib
    if python -c "import talib" 2>/dev/null; then
        log_info "TA-Lib Python binding fixed."
    else
        log_error "TA-Lib Python binding still failing. Check C library installation."
        exit 1
    fi
fi

# Verify other critical imports
python -c "
import ccxt, pandas, numpy, pydantic, mcp
print('All critical packages importable.')
" || {
    log_error "Some critical packages failed to import."
    exit 1
}
log_info "All critical packages verified."

# -----------------------------------------------------------------------
# 5. Copy .env.example to .env
# -----------------------------------------------------------------------
log_step "Setting up environment file..."

if [ -f "${PROJECT_ROOT}/.env" ]; then
    log_warn ".env file already exists. Skipping copy."
    log_warn "Review .env.example for any new variables."
else
    cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
    log_info ".env created from .env.example"
    log_warn "IMPORTANT: Edit .env with your actual API keys before running the bot!"
fi

# -----------------------------------------------------------------------
# 6. Initialise SQLite databases
# -----------------------------------------------------------------------
log_step "Initialising databases..."

TRADEMEMORY_DB="${PROJECT_ROOT}/user_data/tradememory.db"
if [ ! -f "$TRADEMEMORY_DB" ]; then
    sqlite3 "$TRADEMEMORY_DB" <<'SQL'
-- Trade history
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    amount REAL NOT NULL,
    leverage INTEGER DEFAULT 1,
    pnl REAL,
    fees REAL DEFAULT 0,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    circuit_breaker_level TEXT,
    regime TEXT,
    confidence INTEGER,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Balance snapshots for equity curve
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance REAL NOT NULL,
    unrealised_pnl REAL DEFAULT 0,
    margin_used REAL DEFAULT 0,
    timestamp TEXT DEFAULT (datetime('now'))
);

-- Daily summaries for performance tracking
CREATE TABLE IF NOT EXISTS daily_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT UNIQUE NOT NULL,
    start_balance REAL NOT NULL,
    end_balance REAL NOT NULL,
    pnl REAL NOT NULL,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    circuit_breaker_level TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Per-strategy / per-regime performance cache
CREATE TABLE IF NOT EXISTS strategy_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    avg_hold_time_minutes REAL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(strategy, symbol, regime)
);

-- Agent decision log for auditing
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    input_summary TEXT,
    output_summary TEXT,
    reasoning TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_balance_timestamp ON balance_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summaries(date);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_ts ON agent_decisions(timestamp);
SQL
    log_info "TradeMemory database created at ${TRADEMEMORY_DB}"
else
    log_info "TradeMemory database already exists."
fi

# -----------------------------------------------------------------------
# 7. Create directory structure and state files
# -----------------------------------------------------------------------
log_step "Creating directory structure..."

DIRS=(
    "user_data/agent_state"
    "user_data/data"
    "user_data/logs"
    "user_data/backtest_results"
    "user_data/strategies"
    "user_data/freqaimodels"
    "docs/reports"
    "docs/plans"
)

for dir in "${DIRS[@]}"; do
    mkdir -p "${PROJECT_ROOT}/${dir}"
done

log_info "Directory structure verified."

# Create empty JSON state files if they do not exist
declare -A STATE_FILES=(
    ["user_data/agent_state/trade_log.json"]='{"trades": []}'
    ["user_data/agent_state/trade_results.json"]='{"trades": []}'
    ["user_data/agent_state/daily_state.json"]='{}'
    ["user_data/agent_state/balance_history.json"]='{"snapshots": []}'
)

for state_file in "${!STATE_FILES[@]}"; do
    filepath="${PROJECT_ROOT}/${state_file}"
    if [ ! -f "$filepath" ]; then
        echo "${STATE_FILES[$state_file]}" > "$filepath"
        log_info "Created ${state_file}"
    fi
done

# -----------------------------------------------------------------------
# 8. Verify exchange connectivity
# -----------------------------------------------------------------------
log_step "Testing exchange connectivity..."

python -c "
import asyncio
import os

async def test_connection():
    import ccxt.async_support as ccxt_async
    testnet = os.getenv('BINANCE_TESTNET', 'true').lower() == 'true'
    exchange = ccxt_async.binanceusdm({'enableRateLimit': True})
    if testnet:
        exchange.set_sandbox_mode(True)
    try:
        await exchange.load_markets()
        ticker = await exchange.fetch_ticker('BTC/USDT:USDT')
        print(f'Connection OK. BTC/USDT last price: \${ticker[\"last\"]:.2f} (testnet={testnet})')
        return True
    except Exception as e:
        print(f'Connection test info: {e}')
        print('This is OK - data will be fetched when the bot starts.')
        return False
    finally:
        await exchange.close()

asyncio.run(test_connection())
" 2>/dev/null || log_warn "Exchange connection test skipped (keys not configured yet)."

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
echo ""
echo "============================================"
echo -e "  ${GREEN}Setup complete!${NC}"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Binance and Anthropic API keys"
echo "  2. Activate the virtual environment:"
echo "       source .venv/bin/activate"
echo "  3. Run tests:"
echo "       pytest tests/ -v"
echo "  4. Start the bot:"
echo "       ./scripts/start_bot.sh --docker   (Docker mode)"
echo "       ./scripts/start_bot.sh --local    (local mode)"
echo ""
