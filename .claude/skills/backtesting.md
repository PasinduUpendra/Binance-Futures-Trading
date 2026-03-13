---
name: backtesting
description: Run backtests on trading strategies
---

# Backtesting Skill

Run backtests using Freqtrade.

## Steps
1. Ensure historical data is downloaded
2. Configure backtest parameters
3. Run backtest: `freqtrade backtesting --strategy ClaudeQuantAdaptive --timerange YYYYMMDD-YYYYMMDD`
4. Parse results
5. Evaluate: Sharpe > 1.5, drawdown < 40%, win rate > 50%, 100+ trades
6. Report findings

## Commands
```bash
# Download data
freqtrade download-data --exchange binance --pairs BTC/USDT ETH/USDT SOL/USDT --timeframes 5m 15m 1h --days 180

# Run backtest
freqtrade backtesting --strategy ClaudeQuantAdaptive --timerange 20240701-20241231

# Hyperopt
freqtrade hyperopt --hyperopt-loss SharpeHyperOptLossDaily --strategy ClaudeQuantAdaptive --epochs 500
```
