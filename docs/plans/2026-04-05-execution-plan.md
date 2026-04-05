# EXECUTION PLAN — Post Market Analysis Fixes

> **Created**: 2026-04-05
> **Based on**: [docs/reports/2026-04-05-market-analysis.md](../reports/2026-04-05-market-analysis.md)
> **Scope**: 6 fixes ranked by impact, each with exact code changes, tests, and DoD

---

## Priority Order

| # | Fix | Impact | Risk | Effort |
|---|-----|--------|------|--------|
| 1 | **Clean duplicate orders + restart bot** | Immediate — stops -$333 bleed | LOW | 30 min |
| 2 | **Fix trade exit recording** | Enables accurate performance tracking | LOW | Medium |
| 3 | **Add daily/weekly trend filter** | Prevents wrong-direction entries | MEDIUM | Medium |
| 4 | **Guard position swaps** | Stops swapping winners | LOW | Small |
| 5 | **Widen trailing stop in strong trends** | Captures more profit on winners | MEDIUM | Small |
| 6 | **Add ranging market strategy** | Addresses 91% idle time | HIGH | Large |

---

## Fix 1: Clean Duplicate Orders + Restart Bot with Fix

### What
Cancel the 79 accumulated duplicate orders (48 LINK + 31 ADA) and restart the bot with the price-based SL/TP detection fix already applied.

### Exact Steps
```bash
# 1. Stop the current bot
kill -TERM $(cat user_data/logs/bot_pid.txt 2>/dev/null) 2>/dev/null || kill -TERM 47210

# 2. Clean duplicate orders via exchange
.venv/bin/python -c "
import asyncio, os, ccxt.async_support as ccxt_async
from dotenv import load_dotenv
load_dotenv()
async def main():
    ex = ccxt_async.binanceusdm({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })
    ex.enable_demo_trading(True)
    for sym in ['LINK/USDT:USDT', 'ADA/USDT:USDT']:
        orders = await ex.fetch_open_orders(sym, params={'trigger': True})
        print(f'{sym}: {len(orders)} orders — cancelling all')
        await ex.cancel_all_orders(sym)
    # Verify
    for sym in ['LINK/USDT:USDT', 'ADA/USDT:USDT']:
        orders = await ex.fetch_open_orders(sym, params={'trigger': True})
        print(f'{sym}: {len(orders)} orders remaining')
    await ex.close()
asyncio.run(main())
"

# 3. Restart bot
nohup .venv/bin/python scripts/run_bot.py > user_data/logs/bot_stdout.log 2>&1 &
echo $! > user_data/logs/bot_pid.txt
```

### Definition of Done
- [ ] 0 duplicate orders on LINK and ADA
- [ ] Bot running with new PID
- [ ] First cycle log shows price-based SL/TP detection working (no "missing SL" false positives)
- [ ] 488 tests passing

### Verification
```bash
# Verify no duplicate orders
.venv/bin/python -c "..." # check order count per symbol
# Verify bot is running
tail -20 user_data/logs/bot_stdout.log
# Verify no false "missing SL" in logs after 2 cycles
grep "missing SL" user_data/logs/bot.log | tail -5
```

### No-Touch Boundaries
- Do NOT change strategy parameters
- Do NOT change circuit breaker thresholds
- Do NOT close the losing positions (ADA/LINK) — let the bot manage them with the fixed reconciliation

---

## Fix 2: Trade Exit Recording

### Problem
7/9 trades in `trade_journal.db` have `exit_price=None, pnl=None`. When positions close (SL/TP hit, trailing stop, time exit, swap), the exit is not recorded back to the trade journal.

### Root Cause
Exits happen via:
1. Exchange-side SL/TP triggers (no callback to update DB)
2. Bot-side trailing stop / time exit / swap (calls order_manager but doesn't update trade journal)
3. Reconciliation detects position gone (orphan cleanup) but doesn't record exit

### Exact Code Change
**File**: `src/orchestrator/main.py`

In `_reconcile_positions_and_orders()`, add position-closed detection:

```python
# After fetching open positions, compare with previous known positions
# For any position that was open last cycle but is now gone, record the exit
for symbol, ts_state in list(self._trailing_stops.items()):
    if symbol not in {p.symbol for p in open_positions}:
        # Position was closed — record exit in trade journal
        try:
            # Fetch the last trade for this symbol to get fill price
            trades = await self.order_manager.exchange.fetch_my_trades(
                symbol, limit=5,
            )
            if trades:
                last_trade = trades[-1]
                exit_price = Decimal(str(last_trade['price']))
                # Calculate PnL
                entry = Decimal(str(ts_state.entry_price))
                size = ... # from last trade
                pnl = (exit_price - entry) * size if ts_state.direction == 'long' else (entry - exit_price) * size
                self.trade_journal.update_trade_exit(symbol, exit_price, pnl)
        except Exception as e:
            logger.error("Failed to record exit for %s: %s", symbol, e)
```

Also need `trade_journal.update_trade_exit()` method.

### Tests Required
- `test_reconcile_records_exit_when_position_closed`
- `test_trade_journal_update_exit`
- `test_exit_recording_with_fetch_my_trades_failure`

### Definition of Done
- [ ] All closed positions get exit_price and pnl recorded within 1 cycle
- [ ] `trade_journal.get_recent_trades()` returns accurate pnl for sizing decisions
- [ ] New tests pass

### No-Touch Boundaries
- Do NOT change the trailing stop logic
- Do NOT modify order placement

---

## Fix 3: Higher-Timeframe Trend Filter

### Problem
Bot takes 4H Supertrend flips without checking if the daily trend supports the direction. This caused LINK long (Mar 30, -$185) and ADA long (Apr 4, -$147) — both against the dominant bearish market.

### Evidence
- Market was bearish for all 9 pairs over 15 days (-7.8% average)
- LINK long and ADA long both opened into declining markets because the 4H Supertrend briefly flipped bullish
- No filter to check "is the bigger picture bullish or bearish?"

### Exact Code Change
**File**: `src/strategies/supertrend_trend.py` — `generate_signal()` method

Add after ADX check:
```python
# Daily trend filter: EMA50 slope over last 5 4H bars (= ~1 day)
# If slope is against signal direction, reduce confidence by 30 points
ema50_values = df_4h['ema_50'].dropna().iloc[-5:]
if len(ema50_values) >= 5:
    ema50_slope = (ema50_values.iloc[-1] - ema50_values.iloc[0]) / ema50_values.iloc[0]
    if direction == SignalDirection.LONG and ema50_slope < -0.005:
        # Going long but EMA50 is declining — headwind
        confidence -= 30
        reasoning += f" EMA50 declining ({ema50_slope:.4f}), -30 confidence."
    elif direction == SignalDirection.SHORT and ema50_slope > 0.005:
        # Going short but EMA50 is rising — headwind
        confidence -= 30
        reasoning += f" EMA50 rising ({ema50_slope:.4f}), -30 confidence."
```

With the 25% minimum confidence threshold, a 30-point penalty on marginal signals will filter out most against-trend entries while still allowing very high-confidence counter-trend entries.

### Backtest Required
```bash
# Baseline
.venv/bin/python scripts/backtest_v4.py --version v6.14-baseline

# With filter
.venv/bin/python scripts/backtest_v4.py --version v6.15-ema50-filter
```

Must pass: PF > 1.5, WR > 55%, Sharpe > 1.5, Max DD < 15%

### Definition of Done
- [ ] Backtest v6.15 passes all 4 gate checks vs v6.14 baseline
- [ ] EMA50 slope filter rejects both the LINK long (Mar 30) and ADA long (Apr 4) scenarios in replay
- [ ] Tests for generate_signal with declining EMA50

### No-Touch Boundaries
- Do NOT change Supertrend parameters (period=8, multiplier=2.0)
- Do NOT change SL/TP ATR multipliers
- Do NOT change ADX threshold

---

## Fix 4: Position Swap Guard

### Problem
Bot's swap logic closed a winning ADA short (-$3 swap_out) when it should have kept it. ADA then dropped 10.8% — the trade would have been a big winner.

### Evidence
- ADA short 0.2606 (Mar 26), swap_out at loss
- ADA went to 0.2324 after swap — missed 10.8% drop

### Exact Code Change
**File**: `src/orchestrator/main.py` — `_find_swap_candidate()` method

Add guard:
```python
# Never swap a position with positive unrealized PnL
if float(pos.unrealized_pnl) > 0:
    continue  # Skip profitable positions as swap candidates
```

### Definition of Done
- [ ] `_find_swap_candidate` skips positions where uPnL > 0
- [ ] Test: `test_swap_skips_profitable_position`

---

## Fix 5: Dynamic Trailing Stop Width

### Problem
Trailing stop exits too early on strong-trend winners. AVAX short exited at 8.94 but went to 8.37 (missed $145+ additional profit at 6x leverage).

### Evidence
Current: trail_distance = 2.5× ATR always
AVAX short: trailed at 2.5× ATR(4H), exited before the full move completed.

### Exact Code Change
**File**: `src/orchestrator/main.py` — `_manage_trailing_stops()` method

```python
# Dynamic trail: if ADX > 30 (strong trend), use wider trail
adx = self._safe_last(df_4h['adx'])
if adx > 30:
    trail_mult = 3.5  # Wider in strong trends
elif adx > 25:
    trail_mult = 3.0
else:
    trail_mult = 2.5  # Current default
```

### Backtest Required
Must pass all gate checks vs baseline.

### Definition of Done
- [ ] Backtest shows improved average win size without hurting overall metrics
- [ ] Test: `test_trailing_stop_wider_in_strong_trend`

---

## Fix 6: Ranging Market Strategy (RESEARCH PHASE)

### Problem
Bot is idle 91.4% of the time because 7/9 pairs are in ranging regime with no active strategy.

### Evidence
- 501/548 cycles: no signal generated
- MeanReversion disabled: 5.3% WR historical
- Most pairs ADX 10-18 (ranging)

### Approach
This is NOT a quick fix. It requires the full strategy versioning pipeline (CLAUDE.md §8):

1. **Research Phase** (1 week):
   - Backtest GridBot/DCA strategy for ranging markets
   - Backtest improved MeanReversion with tighter BB (1.5σ instead of 2.0σ)
   - Evaluate RSI divergence entries in ranging markets

2. **Validation Phase**:
   - PF > 1.5, WR > 55%, Sharpe > 1.5, Max DD < 15%
   - Walk-forward out-of-sample test
   - Must not degrade SupertrendTrend performance

3. **Paper Trading Phase**:
   - Run alongside SupertrendTrend for 2 weeks minimum
   - Monitor and compare

### Definition of Done
- [ ] Backtest evidence for at least 1 ranging strategy passing all gates
- [ ] 2-week paper trading matching backtest within ±20%
- [ ] Documented in CHANGELOG, config, SSOT

### No-Touch Boundaries
- Do NOT enable MeanReversion without backtest evidence
- Do NOT change SupertrendTrend logic while evaluating ranging strategies
- Do NOT exceed $30 hard floor or any immutable rule
