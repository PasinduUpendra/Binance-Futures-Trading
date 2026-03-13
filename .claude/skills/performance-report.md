---
name: performance-report
description: Generate trading performance report
---

# Performance Report Skill

Generate a comprehensive performance report.

## Steps
1. Fetch account balance history
2. Get all trades from TradeMemory/journal
3. Calculate: daily P&L, cumulative P&L, win rate, Sharpe ratio
4. Break down by strategy and regime
5. Calculate doubling progress vs target
6. Generate report markdown
7. Save to docs/reports/

## Metrics
- Total P&L ($ and %)
- Win rate (overall and by strategy)
- Average win vs average loss
- Sharpe ratio (30-day rolling)
- Max drawdown
- Doubling progress (current vs $75 * 2^(days/10))
