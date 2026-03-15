---
name: backtesting
description: >
  Run, analyze, and validate backtests for Claude Quant trading strategies.
  Covers v4 production-code backtesting, walk-forward validation, strategy versioning pipeline,
  and result interpretation. Full reference: .github/skills/backtest-expert/SKILL.md
---

# Backtesting Skill

## CRITICAL: Always use v4 backtest (`scripts/backtest_v4.py`)
v4 uses PRODUCTION code classes (AdaptiveStrategy, PositionSizer, LeverageManager, GARCH).
Earlier versions (v1-v3) are retained for reference only — they use inline logic that diverges from production.

## Quick Commands
```bash
# Activate environment + run v4 backtest
source .venv/bin/activate && python scripts/backtest_v4.py

# Run with output capture
python scripts/backtest_v4.py 2>&1 | tee user_data/backtest_results/v4_$(date +%Y%m%d_%H%M).txt

# Run full test suite FIRST, then backtest
python -m pytest tests/ -v && python scripts/backtest_v4.py
```

## Pipeline Thresholds (must ALL pass)
| Metric | Minimum | v4 Result |
|--------|---------|-----------|
| Profit Factor | > 1.5 | 5.39 |
| Win Rate | > 55% | 69.2% |
| Sharpe Ratio | > 1.5 | 3.98 |
| Max Drawdown | < 15% | TBD |
| Trade Count | ≥ 30 | 39 |

## Walk-Forward Validation
1. Split data: 70% in-sample, 30% out-of-sample
2. Run v4 on IS → record metrics
3. Run v4 on OOS → metrics must be ≥ 80% of IS

## Strategy Versioning Pipeline
```
Unit Tests (100% pass)
  → Backtest v4 (PF>1.5, WR>55%, Sharpe>1.5, MaxDD<15%)
    → Walk-Forward OOS (PF>1.2, no degradation)
      → Paper Trading (within ±20% of backtest)
        → Live (auto-rollback if WR<50% over 20 trades)
```

## Full Reference
See `.github/skills/backtest-expert/SKILL.md` for comprehensive documentation.
