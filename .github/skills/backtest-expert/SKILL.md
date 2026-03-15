---
name: backtest-expert
description: >
  Backtesting expertise for Claude Quant. Use when: running backtests, analyzing results, validating
  strategies, walk-forward analysis, comparing backtest versions, interpreting metrics (Sharpe, PF,
  drawdown, win rate), verifying production-code fidelity, or debugging strategy divergences.
applyTo: "scripts/backtest*.py,src/strategies/**,tests/**,user_data/backtest_results/**"
---

# Backtest Expert — Comprehensive Skill

## Scope

This skill covers ALL backtesting operations for Claude Quant: running backtests, interpreting results,
validating strategy changes, production-code fidelity, walk-forward testing, and the strategy versioning pipeline.

---

## 1. Backtest Architecture

### Backtest Versions (Evolution History)

| Version | Script | Key Difference | Status |
|---------|--------|----------------|--------|
| v1 | `backtest.py` | Original Freqtrade-based | Legacy |
| v2 | `backtest_v2.py` | Custom with basic strategies | Superseded |
| v3 | `backtest_v3.py` | Inline 4H Supertrend + BB MR | Reference only |
| v4 | `backtest_v4.py` | **PRODUCTION CODE paths** | **CURRENT** |

### v4 Backtest (AUTHORITATIVE)

**v4 is the ONLY valid backtest**. It uses actual production classes:

```python
# Production components used directly:
from src.strategies.adaptive_strategy import AdaptiveStrategy  # Regime routing + signal gen
from src.risk.position_sizer import PositionSizer              # Half-Kelly + CB multipliers
from src.risk.leverage_manager import LeverageManager           # Confidence/regime/CB lookup
from src.risk.volatility_model import VolatilityModel           # GARCH scaling
from src.risk.circuit_breaker import CircuitBreaker              # Full CB gate
from src.execution.fee_calculator import FeeCalculator           # Binance VIP 0 fees
from src.data.indicator_engine import IndicatorEngine            # TA calculations
```

**Why v4 matters**: v3 used inline logic that diverged from production code. The v3→v4 discovery
(CHANGELOG 2026-03-15) showed v3's +94% was inflated because it used different confidence formulas
and simpler sizing. v4's +172.9% over 172 days (39 trades) is the validated truth.

---

## 2. Running a Backtest

### Prerequisites

```bash
# 1. Ensure historical data exists
ls user_data/data/ETH_USDT_USDT_4h.json
ls user_data/data/SOL_USDT_USDT_4h.json
ls user_data/data/DOGE_USDT_USDT_4h.json

# 2. Activate virtual environment
source .venv/bin/activate

# 3. Run v4 backtest (ALWAYS use v4)
python scripts/backtest_v4.py
```

### Data Requirements

| Pair | Timeframes | Min Candles | Source |
|------|-----------|-------------|--------|
| ETH/USDT:USDT | 4H, 1H | 200 warmup + test period | `user_data/data/` |
| SOL/USDT:USDT | 4H, 1H | 200 warmup + test period | `user_data/data/` |
| DOGE/USDT:USDT | 4H, 1H | 200 warmup + test period | `user_data/data/` |

Data format: JSON arrays with `[timestamp_ms, open, high, low, close, volume]`

---

## 3. Interpreting Backtest Results

### Key Metrics & Thresholds (Strategy Versioning Pipeline)

| Metric | Minimum | Good | Excellent | v4 Result |
|--------|---------|------|-----------|-----------|
| **Profit Factor** | > 1.5 | > 2.0 | > 3.0 | 5.39 |
| **Win Rate** | > 55% | > 60% | > 70% | 69.2% |
| **Sharpe Ratio** | > 1.5 | > 2.5 | > 3.5 | 3.98 |
| **Max Drawdown** | < 15% | < 10% | < 5% | TBD |
| **Avg Daily Return** | > 0.3% | > 0.5% | > 1.0% | 0.628% |
| **Trade Count** | > 30 | > 50 | > 100 | 39 |

### Metric Definitions

```
Profit Factor = Gross Profit / Gross Loss
  → PF > 1.0 = profitable; PF > 2.0 = strong edge

Win Rate = Winning Trades / Total Trades
  → With 2:1 R/R, even 40% WR is profitable. 69% + 2:1 R/R = exceptional

Sharpe Ratio = (Mean Return - Risk Free Rate) / Std Dev of Returns
  → Annualized. > 2.0 is excellent. > 3.0 is outstanding.

Max Drawdown = Largest peak-to-trough decline in equity curve
  → < 15% required for pipeline. < 10% preferred.

Expectancy = (WR × Avg Win) - ((1 - WR) × Avg Loss)
  → Must be positive. Higher = stronger edge.
```

### What Makes a Backtest Reliable

| Factor | Requirement |
|--------|------------|
| Trade count | ≥ 30 minimum for statistical significance |
| Time span | ≥ 3 months for trend strategies |
| Regime diversity | Must cover trending + ranging + volatile periods |
| Fee modeling | Must include 0.07-0.10% round-trip fees |
| Slippage | Should include 0.05-0.10% slippage estimate |
| Funding | Multi-day holds should model 0.01%/8h funding |
| Production fidelity | Use actual production classes (v4 pattern) |

---

## 4. Backtest Configuration

### v4 Backtest Parameters (from `scripts/backtest_v4.py`)

```python
PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]
INITIAL_BALANCE = 68.33        # Match real account
HARD_FLOOR = 30.0              # Circuit breaker DEAD level
MAX_HOLD_BARS = 120            # 120 × 1H = 5 days max hold
TRAIL_ACTIVATE_ATR_MULT = 2.0  # Activate trailing at 2× ATR
TRAIL_ATR_MULT = 2.5           # Trail distance 2.5× ATR
```

### Strategy Parameters (SupertrendTrend v4)

```python
# Entry: 4H Supertrend flip + ADX ≥ 18
# Stop-Loss: 3.0 × ATR(4H) from entry
# Take-Profit: 6.0 × ATR(4H) from entry (R/R = 2.0)
# Trailing: Activate at 2.0× ATR, trail at 2.5× ATR
# Reversal Exit: Close on 4H Supertrend counter-flip
# Confidence: Base flip 40pt + ADX 20pt + EMA align 20pt + RSI 10pt + flip quality 10pt
```

---

## 5. Strategy Versioning Pipeline

**No strategy goes live without passing ALL gates:**

```
Gate 1: Unit Tests     → 100% pass
Gate 2: Backtest (v4)  → PF > 1.5, WR > 55%, Sharpe > 1.5, MaxDD < 15%
Gate 3: Walk-Forward   → OOS PF > 1.2, no degradation
Gate 4: Paper Trading  → Live matched within ±20% of backtest
Gate 5: Live Monitor   → Auto-rollback if WR < 50% over 20 trades
```

### How to Run Walk-Forward Validation

```python
# Split data into in-sample (70%) and out-of-sample (30%)
# Run v4 backtest on IS portion → record metrics
# Run v4 backtest on OOS portion → compare metrics
# OOS metrics must be ≥ 80% of IS metrics to pass

# Example:
# IS: 2025-09-01 to 2026-01-15 → PF 5.4, WR 69%
# OOS: 2026-01-15 to 2026-03-15 → PF must be ≥ 4.3, WR ≥ 55%
```

### Comparing Backtest Versions

When evaluating a strategy change:
1. Run v4 backtest with CURRENT code → record baseline
2. Apply code change
3. Run v4 backtest with NEW code → record comparison
4. Print side-by-side: PF, WR, Sharpe, MaxDD, trade count, daily return
5. Change passes ONLY if ALL metrics improve (or hold) AND no single metric degrades > 10%

---

## 6. Common Backtest Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| **Look-ahead bias** | Unrealistically high WR | Ensure signals use only past data |
| **Survivorship bias** | Only backtesting winners | Test across ALL pairs, including underperformers |
| **Overfitting** | IS great, OOS terrible | Walk-forward validation, keep param count low |
| **Fee amnesia** | Profit disappears with fees | v4 uses FeeCalculator with real Binance rates |
| **Inline logic divergence** | Backtest ≠ production | v3→v4 lesson: ALWAYS use production classes |
| **Small sample size** | < 30 trades = noise | Extend test period, accept lower frequency |
| **Ignoring funding** | Multi-day costs hidden | Model 0.01%/8h for long holds |

---

## 7. Backtest Output Analysis

### Equity Curve Interpretation

```
Strong edge: Smooth upward curve, small drawdowns, consistent recovery
Weak edge: Choppy, deep drawdowns, long recovery periods
Curve-fitting: Perfect IS curve, sharp OOS degradation
```

### Trade Distribution Analysis

```
Healthy: Even spread across time, no clustering
Problematic: All wins in one period, all losses in another (regime dependency)
Good: ~60-70% wins, losses are small and controlled by SL
Bad: Few large wins, many small losses (weak R/R despite high frequency)
```

---

## 8. Key Files

| File | Purpose |
|------|---------|
| `scripts/backtest_v4.py` | AUTHORITATIVE backtest — uses production code |
| `scripts/backtest_v3.py` | Reference — inline logic (superseded) |
| `scripts/backtest_v2.py` | Legacy |
| `scripts/backtest.py` | Original Freqtrade-based |
| `user_data/data/*.json` | Historical OHLCV data |
| `user_data/backtest_results/` | Saved backtest outputs |
| `src/strategies/adaptive_strategy.py` | Production strategy router |
| `src/strategies/supertrend_trend.py` | Active strategy |
| `src/risk/position_sizer.py` | Production sizing |
| `src/risk/leverage_manager.py` | Production leverage |
| `src/risk/circuit_breaker.py` | Production CB gates |

---

## 9. Automation Commands

```bash
# Quick backtest
source .venv/bin/activate && python scripts/backtest_v4.py

# Backtest with output capture
python scripts/backtest_v4.py 2>&1 | tee user_data/backtest_results/v4_$(date +%Y%m%d_%H%M).txt

# Compare two runs (manual diff)
diff user_data/backtest_results/v4_baseline.txt user_data/backtest_results/v4_new.txt

# Run all strategy tests first
python -m pytest tests/test_strategies/ -v

# Full test suite then backtest
python -m pytest tests/ -v && python scripts/backtest_v4.py
```
