#!/bin/bash
cd "$(dirname "$0")/.."
.venv/bin/python scripts/backtest_v5_sweep.py --quiet > /tmp/sweep_output.txt 2>&1
echo "SWEEP_DONE exit=$?" >> /tmp/sweep_output.txt
