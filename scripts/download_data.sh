#!/bin/bash
# Claude Quant - Download Historical Data
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

DAYS="${1:-180}"

echo "=== Downloading $DAYS days of historical data ==="

cd "$PROJECT_ROOT"

freqtrade download-data \
    --config config/freqtrade/config_backtest.json \
    --pairs BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT BNB/USDT:USDT XRP/USDT:USDT \
        DOGE/USDT:USDT ADA/USDT:USDT AVAX/USDT:USDT LINK/USDT:USDT MATIC/USDT:USDT \
    --timeframes 5m 15m 1h 4h 1d \
    --days "$DAYS" \
    --userdir user_data

echo "Download complete."
