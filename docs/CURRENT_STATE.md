# CURRENT_STATE.md — Verified Runtime Snapshot

> **Date:** 2026-04-22
> **Method:** Every value below was read directly from source code at the cited file:line. Where docs disagreed with code, code wins. See [DRIFT_MAP.md](DRIFT_MAP.md) for the delta list.
> **Status of bot process:** NOT RUNNING (verified via `pgrep -fl orchestrator.main` → no process).

---

## 1. Environment & Process Reality

| Field | Value | Source |
|---|---|---|
| Exchange account mode | **MAINNET** (`BINANCE_TESTNET=false`) | [.env:10](../.env#L10) |
| Initial capital reference | `$68.33351422` USDT | [.env:23](../.env#L23) |
| Env-level max concurrent positions | `5` (IGNORED by CB which hardcodes 3) | [.env:26](../.env#L26) |
| Orchestrator entry | `src/orchestrator/main.py` | [main.py](../src/orchestrator/main.py) |
| Bot runner | `scripts/run_bot.py` | n/a |
| Process at time of audit | **Not running** | `pgrep` |
| Most recent commit | `55b4d62` — callback_rate rounding + unmatched-exit trade record | `git log` |
| Working tree dirty | `order_manager.py`, `trade_journal.py`, `orchestrator/main.py`, `supertrend_trend.py` modified, plus SQLite WAL/SHM files | `git status` |

---

## 2. Runtime Constants (the numbers that actually drive behavior)

From [main.py:144-181](../src/orchestrator/main.py#L144-L181):

| Constant | Value | Meaning |
|---|---|---|
| `TRADING_PAIRS` | 8 pairs: `ETH, SOL, DOGE, XRP, LINK, AVAX, SUI, ADA` (all `/USDT:USDT`) | BTC is source-commented out despite balance >>$200 |
| `TIMEFRAME_DIRECTION` | `4h` | Regime + SupertrendTrend primary |
| `TIMEFRAME_ENTRY` | `1h` | Entry timing / continuation |
| `TIMEFRAME_FAST` | `15m` | Fast-entry cascade level |
| `CYCLE_INTERVAL_SECONDS` | `1800` (30 min) | Polling interval |
| `MAX_HOLD_BARS` | `100` 1H bars (~4.17 days) | Time-exit cap |
| `WRONG_SIDE_FORCE_CLOSE_CYCLES` | `8` cycles (~4h) | Force-close threshold (CHANGELOG v6.20 claimed 2 — drift) |
| `DYNAMIC_POS_CONFIDENCE_MIN` | `60.0` | Gate for +1 position above CB cap |
| `DYNAMIC_POS_BALANCE_PER_SLOT` | `$15` | Required balance per extra slot |
| `DYNAMIC_POS_ABSOLUTE_MAX` | `5` | Never more than 5, regardless of CB |
| `MIN_NOTIONAL` | BTC=$100, ETH=$20, LINK=$20, default=$5 | Per-pair Binance minimum |

---

## 3. What The Bot Actually Does Each Cycle (verified)

Per [`_run_cycle()`](../src/orchestrator/main.py#L398) in `main.py`:

**Step 1 — Sentinel** ([main.py:411-458](../src/orchestrator/main.py#L411-L458))
- `get_margin_balance()` (equity, not wallet) → `DrawdownMonitor.update()`
- `CircuitBreaker.is_trading_allowed(balance, recent_trades, start_of_day, peak)` — single authoritative gate
- Drawdown-from-peak tightens level only (15% → YELLOW, 30% → RED, 50% → DEAD) — never loosens ([circuit_breaker.py:49-51, 155-172](../src/risk/circuit_breaker.py#L49-L172))

**Step 1b — Data fetch** ([main.py:460-500](../src/orchestrator/main.py#L460-L500))
- For each of 8 pairs: `fetch_ohlcv(4h,200)` → **drop last in-progress candle** → `fetch_ohlcv(1h,200)` → `fetch_ohlcv(15m,200)`
- `DataValidator.validate_ohlcv` on each; on failure skip pair silently (only warn)
- `IndicatorEngine.calculate_all` computes all indicators (EMA9/21/50/200, RSI, MACD, ADX, BB, ATR, Supertrend(8,2.0), Volume SMA, Z-Score)

**Step 1c — Reconciliation** ([main.py:502-507](../src/orchestrator/main.py#L502))
- `_reconcile_positions_and_orders()`: inspects conditional (algo) orders via `fapiPrivate`. Emergency SL placement for unprotected positions. Orphan order purge. Excess position closer if count > CB cap.

**Step 2 — Supertrend reversal exit (TIGHTEN TO BREAKEVEN, not close)** ([main.py:509-518](../src/orchestrator/main.py#L509))
- Cancels SL+TP, re-places SL at entry (breakeven), re-places TP
- Dedup check: skips if current SL already within 0.1% of entry

**Step 2b — Trailing stop management** ([main.py:520-525](../src/orchestrator/main.py#L520))
- Python-side trail at 2.0 ATR activate / 2.5 ATR trail (primary)
- Partial TP at 1:1 R/R (50% scale out, move SL to breakeven)
- Position-level PnL alerts (WARNING at -3% margin, ALERT at -5% margin)

**Step 2c — Time-based exit** ([main.py:527-532](../src/orchestrator/main.py#L527))
- Force close at market when bars_held ≥ 100

**Step 2d — Wrong-side force close** ([main.py:534-545](../src/orchestrator/main.py#L534))
- Per-position counter of cycles where ≥60% of signals oppose the held direction
- Threshold = `WRONG_SIDE_FORCE_CLOSE_CYCLES = 8` (4h), NOT 2 as CHANGELOG claims

**Step 3 — Multi-signal generation** ([main.py:547-623](../src/orchestrator/main.py#L547))
- `CrossAssetConsensus.compute(pair_data_4h)` — computes per-pair ±10 pt adjustment
- For each pair: `adaptive_strategy.get_signal_multi_tf(df_4h, df_1h, df_15m)`
- Skip same-direction signals for already-positioned pairs
- Apply consensus adjustment to confidence
- Collect ALL valid signals (not just best) — sort by confidence descending

**Steps 4-7 — Execute each signal under `_execution_lock`** ([main.py:664-1400+](../src/orchestrator/main.py#L664))
- Re-fetch balance INSIDE the lock (TOCTOU fix)
- Re-run `CircuitBreaker.is_trading_allowed` INSIDE the lock
- Daily trade count gate (Rule #5, 20/day)
- Position count gate with dynamic override (+1 in GREEN if conf≥60 and balance≥$60)
- Smart swap: path 1 (wrong-side, conf≥40) or path 2 (same-side, conf≥50 + 15pt delta)
- Funding rate filter: reject if aligned with funding ≥0.05%; +10 contrarian bonus at ≥0.03% opposite
- Leverage: `LeverageManager.determine_leverage(confidence, regime, cb_level)` then GARCH volatility scale (`forecast` then `forecast_simple` fallback)
- Sizing: 25% / 16.7% / 11.7% by confidence (≥60 / ≥45 / <45) × CB multiplier; Kelly ceiling if ≥10 closed trades
- Min notional per-pair check
- `SanityChecker.check_position_math`
- Liquidation buffer ≥5% via MMR-corrected formula (Tier 1 MMR = 0.4%)
- `PriceValidator.validate_price` (Layer 2) — result flows into audit, not hardcoded
- `SignalValidator.validate_signal` (Layer 3) — cross-checks indicators against raw 4H+1H; skips `entry_type`, `atr_source` metadata keys; R/R epsilon tolerance
- `DecisionAuditor.audit` — REJECT blocks execution
- **Execution**: post-only GTX limit at best bid/ask (5s timeout) → cancel + market fallback on timeout/reject
- Zero-fill guard, None-result guard
- Place SL → if fail, emergency-close naked position
- Place TP → warn on failure, `tp_pending=True`
- Place native Binance TRAILING_STOP_MARKET as safety net (callback=2.5×ATR/price×100, activation=entry±2.0×ATR)
- Record entry in trade journal

---

## 4. Active Strategy Paths

[`AdaptiveStrategy.select_strategy`](../src/strategies/adaptive_strategy.py#L86) maps regime → strategy. ON-DISK strategies vs LIVE routing:

| Regime | ADX | Live route | Source |
|---|---|---|---|
| TRENDING | ≥18 | **SupertrendTrend** (4H) | [adaptive_strategy.py:102](../src/strategies/adaptive_strategy.py#L102) |
| TRENDING | <18 | NO TRADE | [adaptive_strategy.py:108](../src/strategies/adaptive_strategy.py#L108) |
| RANGING | ≥18 | SupertrendTrend (dead-zone bridge) | [adaptive_strategy.py:119](../src/strategies/adaptive_strategy.py#L119) |
| RANGING | <18 | **AdaptiveTrend** (momentum) | [adaptive_strategy.py:126-131](../src/strategies/adaptive_strategy.py#L126) |
| VOLATILE | ≥15 | **BreakoutTrader** (1H) | [adaptive_strategy.py:137](../src/strategies/adaptive_strategy.py#L137) |
| VOLATILE | <15 | NO TRADE | [adaptive_strategy.py:144](../src/strategies/adaptive_strategy.py#L144) |
| QUIET | any | NO TRADE | [adaptive_strategy.py:93](../src/strategies/adaptive_strategy.py#L93) |

**Present on disk but never routed:** `mean_reversion.py`, `trend_follower.py`, `scalper.py`. Source remains for resurrection; orchestrator never invokes them.

**Adaptive Strategy confidence gate:** `MIN_CONFIDENCE = 45.0` ([adaptive_strategy.py:67](../src/strategies/adaptive_strategy.py#L67)). Raised from 25 in Apr 2026 audit because cascade signals (continuation/fast/aligned) at 25% produced 24.4% live win rate.

**SupertrendTrend signal cascade** ([supertrend_trend.py](../src/strategies/supertrend_trend.py)) — tries in order, stops at first non-NONE:

1. **4H flip**: strict prev≠cur on exact last candle; max confidence 100. ADX ≥18 gate.
2. **1H continuation**: 4H established (3+ bars same dir) + 1H flip within last **8 bars** (`CONTINUATION_LOOKBACK_1H`); confidence ceiling 80; staleness decay (−10% per bar over age 1).
3. **15m fast**: 4H established + 1H aligned + 15m flip within last **3 bars**; confidence ceiling 70; staleness decay.
4. **Aligned trend**: 4H established + 1H aligned + EMA aligned + RSI pullback recovery (long: min<55 and current≥55; short: max>45 and current≤45) + volume near average; confidence ceiling 55.

SL/TP multipliers by regime ([supertrend_trend.py:79-84](../src/strategies/supertrend_trend.py#L79-L84)): trending (3.0/6.0), volatile (4.0/8.0), ranging (2.5/5.0), quiet (2.0/4.0). All R/R ≥ 2.0 with epsilon tolerance (`MIN_RR = 2.0 - 1e-9`). For cascade entries ATR uses **4H** (not 1H or 15m) to match backtest.

---

## 5. Active Persistence Model

**Primary database** (WAL mode, synchronous=FULL): `user_data/claude_quant.db` — [database.py](../src/data/database.py)

| Table | Purpose | Row cardinality |
|---|---|---|
| `trades` | Trade journal (entries + exits) | 1 per trade_id |
| `daily_reports` | Daily P&L snapshots | 1 per UTC date |
| `cycle_history` | Every orchestrator cycle | 1 per cycle (blob: `trade_details`, `positions_closed`, `errors`) |
| `system_state` | Key-value (drawdown peak, etc.) | ~10 |
| `strategy_metrics` | Cached perf per (strategy, regime) | small |
| `trailing_stops` | Live TS state (ACID persistence) | 1 per open position |

**Candle cache**: separate `candle_store.py`, table-per-(symbol, timeframe) pattern. Different DB file.

**Legacy JSON state files still on disk** in `user_data/agent_state/`:
- `trailing_stops.json` (legacy fallback — SQLite is primary since v6.10)
- `daily_state.json` (legacy fallback)
- `drawdown_state.json` (legacy atomic write)
- `last_cycle.json` (agent state snapshot)
- `watchdog_state.json`
- `trade_journal.db`, `.db-shm`, `.db-wal` (legacy, still in git status)

**Separate TradeMemory DB**: `TRADEMEMORY_DB_PATH=./user_data/tradememory.db` — MCP client ([.env:30](../.env#L30)). Not integrated into the orchestrator decision loop.

---

## 6. Anti-Hallucination Stack (actually wired)

| Layer | Module | Wired? | Where called |
|---|---|---|---|
| 1 Data | `data_validator.py` | YES | [main.py:474, 484, 494](../src/orchestrator/main.py#L474) |
| 2 Price | `price_validator.py` | YES (v6.21) | [main.py:963](../src/orchestrator/main.py#L963) |
| 3 Signal | `signal_validator.py` | YES (v6.21) | [main.py:981+](../src/orchestrator/main.py#L981) |
| 4 Audit | `decision_auditor.py` | YES — REJECT blocks trade | [main.py ~1064](../src/orchestrator/main.py#L1064) |
| 5 Execution verification | `order_manager.py` (separate GET after POST) | YES | [order_manager.py] |

---

## 7. Circuit Breaker — actual code behavior

From [`circuit_breaker.py`](../src/risk/circuit_breaker.py):

| Level | Balance | Max Lev | Max Pos | Size Mult | Trade |
|---|---|---|---|---|---|
| GREEN | ≥ $60 | 10 | 3 | 1.0 | YES |
| YELLOW | ≥ $45 | 5 | 2 | 0.5 | YES |
| RED | ≥ $30 | 3 | 1 | 0.25 | Conditional (win-rate ≥2/3 of last 10) |
| DEAD | < $30 | 0 | 0 | 0 | HALT |

Additional gates enforced in `is_trading_allowed`:
- Daily loss ≥ 10% of start-of-day balance → override to `trading_allowed=False` for remainder of UTC day
- 5 consecutive losses → 2h pause anchored to last-loss close time
- Peak-drawdown override: 15% → YELLOW, 30% → RED, 50% → DEAD (tightens only)

**Dynamic position override** (orchestrator-level, does NOT mutate CB constants): `_get_effective_max_positions(constraints, confidence, balance)` → `base+1` iff GREEN AND `confidence ≥ 60` AND balance covers $15 per extra slot, capped at `DYNAMIC_POS_ABSOLUTE_MAX = 5`. This means **under GREEN the practical cap is 4, not 3** — a functional relaxation of Immutable Rule #3.

---

## 8. Infra Assumptions In Use

- **Runtime**: single-process async Python 3.11+, APScheduler not driving the main loop — asyncio `while not shutdown_event` polling pattern
- **Exchange client**: `ccxt_async.binanceusdm` with `enable_demo_trading(True)` switch controlled by `BINANCE_TESTNET` env var
- **WS**: `subscribe_kline_close` for 4H candles → `_on_4h_close` handler that can also trigger `_execute_signal` (shares `_execution_lock` with cycle)
- **Persistence**: local SQLite only
- **Secrets**: plaintext `.env` on disk with both production AND testnet keys side by side
- **Process supervision**: none built-in. `scripts/stop_bot.sh`/`start_bot.sh` exist; systemd/launchd not configured
- **Monitoring**: `scripts/watchdog_tools.py` CLI + `.claude/agents/watchdog.md` (Claude agent invoked by humans via `@watchdog`)
- **Alerts**: `alert_system.py` with Telegram + Discord; both use placeholder tokens in `.env` — effectively disabled

---

## 9. What's "in the repo" but NOT live

| Thing | Why it's noise |
|---|---|
| `mean_reversion.py`, `trend_follower.py`, `scalper.py` | No regime routes to them |
| `scripts/backtest.py`, `backtest_v2.py`, `backtest_v3.py` | Superseded by `backtest_v4.py` + `backtest_aggressive.py` |
| `user_data/strategies/ClaudeQuantAdaptive.py` | Freqtrade bridge; orchestrator goes direct ccxt |
| `src/mcp_tools/*` | MCP servers; orchestrator doesn't consume them in the cycle |
| `.claude/agents/{orchestrator,sentinel,market-analyst,strategy-selector,risk-manager,execution-agent,memory-agent,daily-reporter}.md` | Stubs dated 2026-03-15; the live orchestrator is a direct Python class, not an agent conductor |
| `docs/SYSTEM_REVIEW.md` (53KB, 2026-03-16) | Stale review before 12+ version bumps |

---

## 10. The One-Line Summary

**Single-process Python bot, mainnet, 8 alt pairs, 30-min polling + 4H WS triggers, 1 primary strategy + 2 auxiliary routes, multi-level signal cascade, SQLite-only state, zero production monitoring, zero live attribution telemetry, docs lag code by 3–6 weeks.**
