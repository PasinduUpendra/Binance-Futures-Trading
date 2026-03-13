#!/bin/bash
# Claude Quant - Run Backtests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== Claude Quant Backtesting ==="

# Default timerange: last 6 months
END_DATE=$(date -u +%Y%m%d)
START_DATE=$(date -u -v-6m +%Y%m%d 2>/dev/null || date -u -d "6 months ago" +%Y%m%d)
TIMERANGE="${1:-$START_DATE-$END_DATE}"

echo "Timerange: $TIMERANGE"

cd "$PROJECT_ROOT"

# Download data first
echo "Downloading historical data..."
freqtrade download-data \
    --config config/freqtrade/config_backtest.json \
    --pairs BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT BNB/USDT:USDT XRP/USDT:USDT \
    --timeframes 5m 15m 1h \
    --timerange "$TIMERANGE" \
    --userdir user_data

# Run backtest
echo ""
echo "Running backtest..."
freqtrade backtesting \
    --config config/freqtrade/config_backtest.json \
    --strategy ClaudeQuantAdaptive \
    --timerange "$TIMERANGE" \
    --userdir user_data \
    --enable-position-stacking \
    --export trades

echo ""
echo "Backtest complete. Results in user_data/backtest_results/"
