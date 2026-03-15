---
name: risk-assessment
description: >
  Comprehensive risk assessment for trading: position sizing, leverage, circuit breakers,
  liquidation buffers, correlation, and volatility modeling. Covers Kelly criterion,
  confidence-based sizing, GARCH, and the full risk gate pipeline.
  Full reference: .github/skills/quant-finance-strategy-risk/SKILL.md
---

# Risk Assessment Skill

Evaluate and size a potential trade using the full production risk pipeline.

## Steps
1. Fetch current account balance from Binance API (NEVER assume)
2. Check circuit breaker level (GREEN/YELLOW/RED/DEAD)
3. Count open positions vs CB max
4. Calculate position size:
   - Confidence ≥ 60% → 15% of balance
   - Confidence ≥ 45% → 10% of balance
   - Else → 7% of balance
   - Apply CB multiplier (GREEN=1.0, YELLOW=0.5, RED=0.25)
5. Determine leverage from confidence × regime × CB table
6. Apply GARCH volatility adjustment (reduce during vol spikes)
7. Verify liquidation buffer ≥ 5% against exchange-reported liquidation price
8. Check correlation with existing positions (max 25% correlated exposure)
9. Verify R/R ≥ 2.0 (SL=3×ATR, TP=6×ATR)
10. Return APPROVE or REJECT with full math

## Hard Limits (IMMUTABLE — Hardcoded in Production)
| Rule | Limit | Code Location |
|------|-------|---------------|
| Max leverage | 10× | `leverage_manager.py` |
| Max positions | 3 (GREEN), 2 (YELLOW), 1 (RED) | `circuit_breaker.py` |
| Max per trade | 15% of balance | `position_sizer.py` |
| HALT threshold | Balance < $30 | `circuit_breaker.py` |
| Liquidation buffer | ≥ 5% | `leverage_manager.py` |
| Min R/R ratio | 2.0 | `supertrend_trend.py` |
| Daily loss halt | > 10% of start-of-day balance | `circuit_breaker.py` |
| Consecutive losses | 5 → 2h pause | `circuit_breaker.py` |

## Leverage Table (Quick Reference)
| Confidence | Trending | Volatile/Ranging |
|-----------|----------|-----------------|
| 80-100% | 7-10× | 5-7× |
| 60-79% | 5-7× | 3-5× |
| 40-59% | 3-5× | 2-3× |
| 25-39% | 2-3× | 1-2× |
| < 25% | NO TRADE | NO TRADE |

CB caps override: YELLOW max 5×, RED max 3×, DEAD 0×

## Full Reference
See `.github/skills/quant-finance-strategy-risk/SKILL.md` for comprehensive documentation.
