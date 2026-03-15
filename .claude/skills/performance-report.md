---
name: performance-report
description: >
  Generate comprehensive trading performance reports with P&L, Sharpe, drawdown,
  win rate, compound rate tracking, and strategy attribution analysis.
---

# Performance Report Skill

Generate comprehensive performance reports tracking the 0.628% validated daily ceiling.

## Steps
1. Fetch account balance history from Binance API
2. Get all trades from `trade_journal.py` + `database.py`
3. Calculate metrics:
   - Daily P&L ($ and %)
   - Cumulative P&L
   - Win rate (overall + by strategy)
   - Sharpe ratio (30-day rolling)
   - Sortino ratio
   - Maximum drawdown (peak-to-trough)
   - Profit factor (gross profit / gross loss)
4. Break down by strategy and regime
5. Calculate compound rate vs validated ceiling (0.628%) and aspirational target (1.0%)
6. Track equity curve and high-water marks
7. Generate report markdown
8. Save to `docs/reports/`

## Key Metrics
| Metric | Formula | Target |
|--------|---------|--------|
| Daily P&L | Today's balance - yesterday's | Positive |
| Daily compound rate | (balance / initial)^(1/days) - 1 | ≥ 0.628% |
| Win rate | Wins / Total trades | ≥ 55% |
| Sharpe ratio | (mean_ret - rf) / std_ret × √365 | ≥ 1.5 |
| Max drawdown | Max peak-to-trough decline | ≤ 15% |
| Profit factor | Gross profit / |Gross loss| | ≥ 1.5 |
| Expectancy | (WR × avg_win) - ((1-WR) × avg_loss) | Positive |

## Compounding Progress
```
Validated: 0.628% daily → $68 → $122 in 90 days → $620 in 365 days (~870% ann.)
Aspirational: 1.0% daily → $68 → $171 in 90 days → $2568 in 365 days (~3600% ann.)
```

## Output Format
```
=== PERFORMANCE REPORT — YYYY-MM-DD ===
Balance: $XX.XX (change: +$X.XX / +X.XX%)
Daily Rate: X.XXX% (validated ceiling: 0.628%, target: 1.000%)
Win Rate: XX.X% (X/Y trades)
Sharpe: X.XX | Sortino: X.XX | PF: X.XX
Max DD: X.XX%
Compound Progress: X days, tracking at XX% of target
```

## Related Skills
- Risk Assessment: `.github/skills/quant-finance-strategy-risk/SKILL.md`
- Backtest Validation: `.github/skills/backtest-expert/SKILL.md`
