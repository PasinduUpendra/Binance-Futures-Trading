---
name: quant-finance-strategy-risk
description: >
  Quantitative finance strategy development and risk assessment. Use when: evaluating trade signals,
  computing risk metrics (Sharpe, Sortino, VaR, CVaR, drawdown), position sizing (Kelly, fractional Kelly),
  regime detection, volatility modeling (GARCH, EWMA), indicator engineering, strategy design, risk budgeting,
  correlation analysis, or any quant finance decision.
applyTo: "src/risk/**,src/strategies/**,src/data/indicator_engine.py,scripts/backtest*.py,config/**"
---

# Quant Finance Strategy + Risk Assessment — Comprehensive Skill

## Scope

This skill covers quantitative finance theory and implementation for Claude Quant: strategy design,
risk measurement, position sizing, volatility modeling, regime detection, and the mathematical
foundations underlying all trading decisions.

---

## 1. Risk Metrics Reference

### Sharpe Ratio

```python
# Annualized Sharpe (standard)
sharpe = (mean_daily_return - risk_free_rate) / std_daily_return * sqrt(365)

# Interpretation:
# < 0.5  → Poor (barely above noise)
# 0.5-1  → Acceptable
# 1-2    → Good
# 2-3    → Very good
# > 3    → Excellent (v4 backtest: 3.98)
```

### Sortino Ratio

```python
# Like Sharpe but only penalizes downside volatility
downside_returns = returns[returns < target_return]
downside_std = sqrt(mean(downside_returns ** 2))
sortino = (mean_return - target_return) / downside_std * sqrt(365)

# Higher is better — captures asymmetric return profiles
```

### Maximum Drawdown

```python
def max_drawdown(equity_curve):
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak
    return drawdown.min()  # Most negative value

# Thresholds for Claude Quant:
# < 10% → Acceptable
# < 15% → Pipeline maximum
# > 15% → Strategy FAILS pipeline, must be redesigned
```

### Value at Risk (VaR) & Conditional VaR (CVaR)

```python
# Historical VaR at 95% confidence
var_95 = returns.quantile(0.05)  # 5th percentile of returns

# Conditional VaR (Expected Shortfall) — average loss beyond VaR
cvar_95 = returns[returns <= var_95].mean()

# Use for: daily risk budgeting, understanding worst-case scenarios
```

### Profit Factor

```python
profit_factor = gross_profit / abs(gross_loss)

# PF > 1.0 → profitable
# PF > 1.5 → pipeline minimum
# PF > 2.0 → strong
# PF > 3.0 → excellent
# PF = 5.39 → v4 backtest result (exceptional)
```

### Expectancy

```python
expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

# With v4 metrics: (0.692 * avg_win) - (0.308 * avg_loss)
# Must be positive for any viable strategy
# Higher expectancy = deploy with larger position sizes
```

---

## 2. Position Sizing Models

### Half-Kelly Criterion (Implemented in `kelly_criterion.py`)

```python
# Kelly fraction for optimal geometric growth:
f_star = (W * R - (1 - W)) / R
# W = win rate, R = reward/risk ratio

# Half-Kelly for safety (halves variance of outcomes):
half_kelly = 0.5 * f_star

# Example: W=0.692, R=2.0
# f* = (0.692 * 2 - 0.308) / 2 = (1.384 - 0.308) / 2 = 0.538
# Half-Kelly = 0.269 = 26.9% of balance
# But capped at 15% per Immutable Rule #4
```

### Confidence-Based Sizing (v4 — Active in Orchestrator)

```python
# Replaced Half-Kelly for $68 account (Kelly always produced ~$5 minimum)
if confidence >= 60%:   position_pct = 15% of balance
elif confidence >= 45%: position_pct = 10%
else:                   position_pct = 7%

position_pct *= CB_size_multiplier  # GREEN=1.0, YELLOW=0.5, RED=0.25
margin = balance * position_pct
margin = max(margin, $5)           # Binance minimum notional
margin = min(margin, balance * 15%) # Hard cap
notional = margin * leverage
```

### Risk Budget Allocation

```python
# Per-trade risk:
max_risk_per_trade = balance * 0.02  # 2% rule
# With SL at 3× ATR and 5× leverage:
# Actual risk = margin * (sl_distance / entry_price) ≈ margin * (3×ATR/price)

# Daily risk budget:
max_daily_loss = balance * 0.10     # 10% daily halt (circuit breaker)

# Portfolio risk:
max_concurrent = 3                   # Green CB level
max_correlated = balance * 0.25     # 25% in correlated assets
```

---

## 3. Volatility Modeling

### GARCH(1,1) (Implemented in `volatility_model.py`)

```python
# GARCH forecasts next-period volatility from:
# σ²_t = ω + α × ε²_{t-1} + β × σ²_{t-1}
# where ε = return residual

# Usage in Claude Quant:
# VolatilityModel.adjust_leverage() reduces leverage during vol spikes
# If GARCH forecast > 1.5× historical avg → reduce leverage by proportional factor
```

### EWMA Fallback

```python
# When GARCH fails to converge (common with < 50 data points):
# σ²_t = λ × σ²_{t-1} + (1 - λ) × r²_{t-1}
# Typical λ = 0.94 (RiskMetrics standard)
```

### ATR (Average True Range)

```python
# ATR = SMA of True Range over N periods
# True Range = max(H-L, |H-C_prev|, |L-C_prev|)

# Usage in Claude Quant:
# SL = 3.0 × ATR(4H)   → Stop-loss distance
# TP = 6.0 × ATR(4H)   → Take-profit distance  (R/R = 2.0)
# Trail activate = 2.0 × ATR(4H)
# Trail distance = 2.5 × ATR(4H)
```

---

## 4. Regime Detection

### ADX-Based Classification (Implemented in `regime_detector.py`)

```python
# ADX (Average Directional Index) measures trend STRENGTH (not direction)
# ADX > 25 → Strong trend
# ADX 18-25 → Moderate trend
# ADX < 18 → Weak/no trend

# Regime classification for Claude Quant:
TRENDING  = ADX >= 18    # → SupertrendTrend active
RANGING   = ADX < 20     # → No trade (MR disabled)
VOLATILE  = 15 < ADX < 30 + high BB width  # → No trade (Breakout disabled)
QUIET     = ADX < 15     # → No trade always
```

### Multi-Timeframe Regime

```python
# 4H timeframe for regime detection (higher confidence)
# Indicators computed on 4H data:
# - ADX(14) for trend strength
# - Supertrend(10, 3) for trend direction
# - Bollinger Bands(20, 2) for volatility regime
# - EMA(9, 21, 50, 200) for trend alignment

# 1H timeframe for entry timing only
# Entry price = 1H close at signal time
```

---

## 5. Strategy Design Framework

### SupertrendTrend (Active — `supertrend_trend.py`)

```
Signal Type: Trend-following reversal
Entry Trigger: 4H Supertrend flips direction + ADX ≥ 18
  LONG: Bearish → Bullish flip
  SHORT: Bullish → Bearish flip

Confidence Scoring (0-100):
  Base flip signal:     40 points
  ADX strength bonus:   up to 20 points (higher ADX = more points)
  EMA alignment:        up to 20 points (9>21>50>200 for long)
  RSI confirmation:     up to 10 points (RSI favoring direction)
  Flip quality:         up to 10 points (clean flip vs noisy)

Risk Management:
  Stop-Loss:  3.0 × ATR(4H) from entry
  Take-Profit: 6.0 × ATR(4H) from entry
  Trailing:   Activate at 2.0 × ATR, trail at 2.5 × ATR
  Reversal:   Immediate close on counter-flip
  Time exit:  120 bars (5 days) max hold
```

### Strategy Evaluation Checklist

Before proposing ANY new strategy:
1. Define signal logic precisely (entry, exit, filters)
2. Define confidence scoring formula
3. Define SL/TP in ATR multiples
4. Ensure R/R ≥ 2.0 (Immutable Rule #9)
5. Backtest with v4 pattern (production classes)
6. Compute: PF, WR, Sharpe, MaxDD, trade count
7. Walk-forward validation (70/30 split)
8. Compare against SupertrendTrend baseline
9. Must BEAT baseline on risk-adjusted basis

---

## 6. Leverage Determination

### Dynamic Leverage Table (from `leverage_manager.py`)

| Confidence | Regime | Leverage Range | Midpoint |
|-----------|--------|----------------|----------|
| 80-100% | TRENDING | 7-10× | 8× |
| 60-79% | TRENDING | 5-7× | 6× |
| 60-79% | VOLATILE/RANGING | 3-5× | 4× |
| 40-59% | TRENDING | 3-5× | 4× |
| 40-59% | VOLATILE/RANGING | 2-3× | 2× |
| 25-39% | TRENDING | 2-3× | 2× |
| 25-39% | VOLATILE | 1-2× | 1× |
| < 25% | Any | 0 | NO TRADE |

CB overrides: YELLOW max 5×, RED max 3×, DEAD 0×
GARCH adjustment: Reduces leverage proportionally during vol spikes

### Liquidation Buffer Calculation

```python
# Heuristic: liquidation_distance ≈ 1 / leverage
# Authoritative: exchange.fetch_positions() → liquidationPrice
# Buffer = abs(entry_price - liquidation_price) / entry_price
# MUST be ≥ 5% (Immutable Rule #10)
# SL must be < 50% of actual liquidation distance
```

---

## 7. Circuit Breaker Integration

### Balance-Based Levels (HARDCODED — `circuit_breaker.py`)

| Level | Balance | Max Leverage | Max Positions | Size Multiplier |
|-------|---------|-------------|---------------|-----------------|
| GREEN | ≥ $60 | 10× | 3 | 1.0× |
| YELLOW | ≥ $45 | 5× | 2 | 0.5× |
| RED | ≥ $30 | 3× | 1 | 0.25× |
| DEAD | < $30 | 0 | 0 | 0 — HALT |

### Temporal Breakers

| Trigger | Action |
|---------|--------|
| Daily loss > 10% | HALT until next UTC day |
| 5 consecutive losses | 2-hour pause |
| RED level | Require ≥ 2/3 WR on last 10 trades |

---

## 8. Correlation Risk Management

### Multi-Position Correlation (from `correlation_monitor.py`)

```python
# Max 25% of balance in correlated assets (correlation > 0.7)
# Crypto pairs are often highly correlated:
# ETH/BTC correlation typically 0.7-0.9
# SOL/ETH correlation typically 0.6-0.8
# DOGE/BTC correlation variable 0.4-0.7

# Before opening 2nd position: check correlation with existing
# If corr > 0.7: treat as same risk, check combined exposure < 25%
```

---

## 9. Performance Attribution

### Daily Compound Rate Tracking

```python
# Validated average: 0.628% daily (v4 backtest)
# Aspirational target: 1.0% daily
# Compounding formula: balance_t = balance_0 × (1 + r)^t

# At 0.628%: $68 → $122 in 90 days, $620 in 365 days (~870% ann.)
# At 1.000%: $68 → $171 in 90 days, $2568 in 365 days (~3600% ann.)
```

### Decomposing Returns

```
Total Return = Market Return + Strategy Alpha + Leverage Effect - Costs
Where:
  Market Return = Underlying asset movement × direction
  Strategy Alpha = Edge from signal selection + timing
  Leverage Effect = Amplification factor
  Costs = Fees (0.07-0.10%) + Funding (0.01%/8h) + Slippage (0.05%)
```

---

## 10. Key Code Files

| File | Quant Function |
|------|---------------|
| `src/risk/circuit_breaker.py` | Balance-based + temporal risk gates |
| `src/risk/leverage_manager.py` | Leverage determination + liq buffer |
| `src/risk/position_sizer.py` | Kelly + confidence-based sizing |
| `src/risk/volatility_model.py` | GARCH(1,1) + EWMA forecasts |
| `src/risk/correlation_monitor.py` | Multi-position correlation risk |
| `src/risk/kelly_criterion.py` | Kelly fraction computation |
| `src/risk/drawdown_monitor.py` | High-water mark + DD tracking |
| `src/strategies/supertrend_trend.py` | Active strategy implementation |
| `src/strategies/adaptive_strategy.py` | Regime routing + multi-TF signals |
| `src/strategies/base_strategy.py` | Signal/direction data models |
| `src/data/indicator_engine.py` | All TA indicator calculations |
| `src/reporting/daily_pnl.py` | Daily P&L + Sharpe computation |
| `config/risk/risk_params.yaml` | Tunable risk parameters |
| `config/regime/regime_params.yaml` | Regime classification thresholds |
