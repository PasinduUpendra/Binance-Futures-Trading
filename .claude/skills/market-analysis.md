---
name: market-analysis
description: >
  Multi-timeframe market analysis on crypto futures pairs. Regime detection, indicator
  computation, signal generation via 4H Supertrend + ADX, and opportunity scoring.
  Full reference: .github/skills/quant-finance-strategy-risk/SKILL.md
---

# Market Analysis Skill

Analyze cryptocurrency futures pairs for trading opportunities using the production pipeline.

## Steps (Orchestrator Steps 1b + 3)
1. Fetch 4H OHLCV (200 candles) for each pair via `market_data.py`
2. Fetch 1H OHLCV (200 candles) for each pair
3. Validate data freshness and integrity (anti-hallucination Layer 1)
4. Calculate ALL indicators on 4H data via `indicator_engine.py`
5. Classify regime via `regime_detector.py` (ADX-based)
6. Route to active strategy: SupertrendTrend (only active route)
7. Generate signal with confidence score (0-100)
8. Entry price from 1H close at signal time

## Active Pairs
- ETH/USDT:USDT
- SOL/USDT:USDT
- DOGE/USDT:USDT

## Indicators (4H Timeframe)
| Indicator | Parameters | Purpose |
|-----------|-----------|---------|
| EMA | 9, 21, 50, 200 | Trend alignment |
| RSI | 14-period | Momentum confirmation |
| ADX | 14-period (+DI/-DI) | Regime classification |
| Supertrend | 10, 3 | Trend direction + flip detection |
| Bollinger Bands | 20, 2 | Volatility regime |
| ATR | 14-period | SL/TP sizing |
| MACD | 12, 26, 9 | Momentum |
| Volume SMA | 20-period | Volume confirmation |

## Regime Classification
| Regime | ADX Condition | Trading Action |
|--------|--------------|---------------|
| TRENDING | ADX ≥ 18 | SupertrendTrend active |
| RANGING | ADX < 20 | NO TRADE (MR disabled) |
| VOLATILE | ADX 15-30 + high BB width | NO TRADE (Breakout disabled) |
| QUIET | ADX < 15 | NO TRADE always |

## Signal Confidence Scoring (SupertrendTrend)
```
Base Supertrend flip:     40 points
ADX strength bonus:       up to 20 points
EMA alignment:            up to 20 points
RSI confirmation:         up to 10 points
Flip quality:             up to 10 points
─────────────────────────────────
Maximum:                  100 points
Minimum for trade:        25 points (< 25 = NO TRADE)
```

## ANTI-HALLUCINATION RULES
- ALL prices MUST come from API calls — NEVER fabricate
- Cross-check: current price within daily high/low range
- Reject data older than 2 candle intervals
- Every indicator computed from real OHLCV data — no estimation

## Full Reference
See `.github/skills/quant-finance-strategy-risk/SKILL.md` for strategy and risk details.
