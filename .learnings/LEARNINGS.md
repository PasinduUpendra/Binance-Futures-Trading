# Learnings Log

Structured knowledge captured from corrections, discoveries, and best practices.
Format: `LRN-YYYYMMDD-XXX | priority | status | area`

---

## LRN-20260313-001 | high | resolved | exchange-api
**Testnet requires separate API keys**
Production Binance API keys do NOT work on testnet (demo-fapi.binance.com). Must generate separate keys at testnet.binancefuture.com. Error: "Invalid API-key, IP, or permissions for action."
- Related: `.env` BINANCE_TESTNET=true setting
- Use `exchange.enable_demo_trading(True)` in ccxt, NOT `set_sandbox_mode()`

## LRN-20260313-002 | high | resolved | data-pipeline
**fetch_ohlcv returns list[dict], not DataFrame**
`MarketDataClient.fetch_ohlcv()` returns `list[dict[str, Any]]`. Must convert to `pd.DataFrame(raw)` before passing to `IndicatorEngine.calculate_all()`. The indicator engine calls `df.columns` which crashes on a list.
- Related: `src/orchestrator/main.py` Step 2

## LRN-20260313-003 | high | resolved | risk
**Regime enum names must match across system**
`LeverageManager.MarketRegime` had `STRONG_TREND/MODERATE_TREND` but `RegimeDetector` produces `trending/ranging/volatile/quiet`. Every leverage lookup fell through to the 2x default. All enums must use the same values: TRENDING, VOLATILE, RANGING, QUIET.
- Related: `src/risk/leverage_manager.py`, `src/strategies/regime_detector.py`

## LRN-20260313-004 | medium | resolved | mcp
**MCP servers must use json.dumps, not str()**
`str(dict)` produces Python repr (`{'key': 'value'}`) which is invalid JSON. All MCP tool responses must use `json.dumps(result, default=str)`.
- Related: All 4 files in `src/mcp_tools/`

## LRN-20260313-005 | medium | resolved | exchange-api
**DOGE/USDT:USDT requires whole number amounts**
Amount precision for DOGE on Binance Futures is 0 decimal places. Orders with fractional DOGE amounts will be rejected.
- Related: `src/execution/order_manager.py`

## LRN-20260313-006 | medium | resolved | fees
**Taker fee is 0.05%, not 0.04%**
Verified via Binance API (`fapiPrivateGetCommissionRate`): taker=0.000500 for all 3 pairs. Code was using 0.0004. Fixed to 0.0005. BNB Burn is enabled on account (10% discount available).
- Related: `src/execution/fee_calculator.py`
- Round-trip cost: 0.10% of notional (taker both sides), 0.09% with BNB discount

## LRN-20260314-007 | critical | resolved | strategy
**4H Supertrend flips are the key entry signal for crypto**
1H EMA crossover trend following has ~36-44% win rate on crypto (negative EV). 4H Supertrend direction flips have 60.9% win rate with 2.58 profit factor over 172 days backtesting.
- Key parameters: 3x ATR(4H) SL, 6x ATR(4H) TP, trailing stop at 2.0 ATR activate / 2.5 ATR trail
- Supertrend reversal exit: individually -$33 loss, but enables capital recycling into new direction → net system +$64
- 4H filters out 1H noise; Supertrend flips are significant regime changes
- Related: `scripts/backtest_v3.py`

## LRN-20260314-008 | critical | resolved | strategy
**Mean reversion works on 4H, not 1H**
1H MR conditions (zscore<-2, RSI<30, close<=bb_lower) simultaneously are too rare in crypto (1.15% of candles). 4H MR with z<-1.5, RSI<35, ADX<22 fires more and has 73% win rate, +$11.22 over 172 days.
- Target: 4H BB middle band (not opposite band)
- 2.0x ATR(4H) SL
- Related: `scripts/backtest_v3.py`

## LRN-20260314-009 | high | resolved | strategy
**MeanReversion on 1H requires 2-of-3 confirmation, not all 3**
Original MR required ALL of: close<=bb_lower, zscore<=-2.0, RSI<=30 simultaneously. In crypto 1H, this only fires 1.15% of the time. Using 2-of-3 with relaxed thresholds (z>=-1.5, RSI<=35) fires 4.53% of the time. BUT: 4H MR is still superior.
- Related: `src/strategies/mean_reversion.py`

## LRN-20260314-010 | high | resolved | risk
**LeverageManager confidence gate must match AdaptiveStrategy gate**
AdaptiveStrategy.MIN_CONFIDENCE was lowered to 25% but LeverageManager still had a hard gate at confidence<40 returning leverage=0. All signals with 25-39% confidence got blocked. Added leverage tiers for 25-39% confidence range.
- Related: `src/risk/leverage_manager.py`

## LRN-20260314-011 | medium | resolved | backtest
**Trailing stop profit locking: activate after 2.0 ATR, trail at 2.5 ATR**
1.0 ATR activate / 1.5 ATR trail was too tight for crypto — winners got stopped out too early (avg win = avg loss). 2.0 ATR activate / 2.5 ATR trail lets winners run while still protecting profit. In backtest v3: 66% of SL exits were winners (trailing stop locked profit).
- Related: `scripts/backtest_v3.py`

## LRN-20260314-012 | medium | resolved | backtest
**Position minimum should be on notional (margin×leverage), not margin**
Binance minimum notional is $5 for DOGE/SOL, $20 for ETH. At 2x leverage, $3 margin = $6 notional (above minimum). Using margin<$5 as the gate incorrectly blocked trades that would have been valid after leverage.
- Related: `scripts/backtest.py`
