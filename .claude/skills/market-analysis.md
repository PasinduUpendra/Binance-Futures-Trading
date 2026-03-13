---
name: market-analysis
description: Perform comprehensive market analysis on a trading pair
---

# Market Analysis Skill

Analyze a cryptocurrency futures pair for trading opportunities.

## Steps
1. Fetch latest OHLCV data (200 candles, 5m timeframe)
2. Calculate all indicators: EMA(9,21,50,200), RSI(14), MACD, ADX(14), Supertrend, BB(20,2), ATR(14)
3. Validate data freshness and integrity
4. Classify market regime (Trending/Ranging/Volatile/Quiet)
5. Identify key support/resistance levels
6. Score opportunity (0-100)

## Usage
```
/market-analysis BTC/USDT:USDT
```

## Output
Structured JSON with regime, indicators, opportunity score, and recommendation.
