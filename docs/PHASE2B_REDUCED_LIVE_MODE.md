# PHASE2B_REDUCED_LIVE_MODE.md

> **Date:** 2026-04-22
> **Scope:** Phase 2B of `docs/MASTER_ROADMAP.md` — narrow the live surface before Phase 1 attribution has enough trades to make evidence-based removals.
> **Rule:** This is a **live-configuration change only**. No thresholds, no CB constants, no strategy files altered.

---

## 1. What changed

A single feature-flag module controls the entire reduced surface:

- **New file:** [`src/orchestrator/reduced_live_mode.py`](../src/orchestrator/reduced_live_mode.py)
- **Master switch:** `REDUCED_LIVE_MODE: bool = True`

When `REDUCED_LIVE_MODE` is `True`, the live bot behaves as below. When set to `False`, every consumer short-circuits to pre-Phase-2B behavior — **no other file needs to change**.

---

## 2. Live symbols

| Symbol | Allowed? | Source |
|---|---|---|
| `SOL/USDT:USDT` | **YES** | [`reduced_live_mode.py::ALLOWED_SYMBOLS`](../src/orchestrator/reduced_live_mode.py) |
| `SUI/USDT:USDT` | **YES** | same |
| `ETH`, `DOGE`, `XRP`, `LINK`, `AVAX`, `ADA`, `BTC` | NO (signals) | same |

**Important:** the *full* `TRADING_PAIRS` universe (8 pairs) is still swept by:
- Startup stale-order cleanup ([`main.py` L382](../src/orchestrator/main.py#L382))
- Orphan-order cleanup in `_reconcile_positions_and_orders` ([`main.py` L2831](../src/orchestrator/main.py#L2831))

Only the signal-generating loops iterate `ACTIVE_TRADING_PAIRS` (the filtered subset):
- 4H kline WS subscription
- Step 1b data fetch
- Step 3 signal generation
- Step 2d wrong-side force-close signal probe

This split guarantees that any lingering orders on previously-allowed pairs keep getting reconciled.

---

## 3. Live entry paths

| Path | Allowed? | Where gated |
|---|---|---|
| 4H Supertrend flip (`generate_signal`) | **YES** | `ALLOW_4H_FLIP = True` |
| 1H continuation (`generate_continuation_signal`) | **YES** | `ALLOW_1H_CONTINUATION = True` |
| 15m fast (`generate_fast_signal`) | **NO** | `ALLOW_15M_FAST = False` — gated in [`adaptive_strategy.py::get_signal_multi_tf`](../src/strategies/adaptive_strategy.py) |
| Aligned-trend RSI-pullback (`generate_aligned_signal`) | **NO** | `ALLOW_ALIGNED_TREND = False` — same site |

---

## 4. Disabled components

| Component | State | Where gated |
|---|---|---|
| `AdaptiveTrend` route (RANGING + ADX<18) | **NO TRADE** | `ALLOW_ADAPTIVE_TREND_ROUTE = False` → `AdaptiveStrategy.select_strategy` returns `None` |
| `BreakoutTrader` route (VOLATILE + ADX≥15) | **NO TRADE** | `ALLOW_BREAKOUT_TRADER_ROUTE = False` → same |
| Cross-asset consensus ±10 pt adjustment | **NEUTRALIZED** | `ALLOW_CONSENSUS_ADJUST = False` → orchestrator skips `cross_asset_consensus.compute()` and uses `consensus_adj = {}`; per-pair lookup reads `0.0` |
| Dynamic +1 position override above CB cap | **DISABLED** | `ALLOW_DYNAMIC_POS_OVERRIDE = False` → `_get_effective_max_positions` returns `constraints.max_positions` unchanged |

---

## 5. Explicitly NOT touched

- Circuit-breaker constants (`_GREEN_MAX_POSITIONS`, `_YELLOW_MAX_LEVERAGE`, etc.)
- `MIN_CONFIDENCE`, `DYNAMIC_POS_CONFIDENCE_MIN`, ADX thresholds, RSI pullback thresholds
- `LeverageManager` tiers
- Funding-rate filter thresholds (`FundingRateFilter`)
- `SupertrendTrend` SL/TP multipliers (`SL_TP_BY_REGIME`)
- Strategy source files — disabled code stays on disk for resurrection
- Infra (persistence, Supabase mirror, launchd, heartbeat)

---

## 6. Files changed

| File | Change |
|---|---|
| `src/orchestrator/reduced_live_mode.py` | **NEW** — flags + helpers |
| `src/orchestrator/main.py` | Import helpers; split `TRADING_PAIRS` (full) vs `ACTIVE_TRADING_PAIRS` (filtered); gate `cross_asset_consensus.compute()`; gate `_get_effective_max_positions` +1 override; 4 loops switched to `ACTIVE_TRADING_PAIRS` (kline subscribe, data fetch, signal gen, wrong-side probe) |
| `src/strategies/adaptive_strategy.py` | Import helpers; gate `AdaptiveTrend` + `BreakoutTrader` route selection; gate `generate_fast_signal` + `generate_aligned_signal` cascade branches |
| `tests/test_integration/test_reduced_live_mode.py` | **NEW** — 20 tests |
| `tests/test_integration/test_orchestrator_fixes.py` | `test_dynamic_pos_limit_green_high_confidence` now patches `ALLOW_DYNAMIC_POS_OVERRIDE=True` to validate full-mode semantics |
| `tests/test_strategies/test_adaptive_multi_tf.py` | `test_ranging_routes_to_adaptive_trend` and `test_volatile_routes_to_breakout_trader` patch the corresponding route flags to validate full-mode semantics |
| `docs/CURRENT_STATE.md` | Updated §2, §4 to reflect reduced-mode live surface + 833-test count |
| `docs/DRIFT_MAP.md` | Items 2 (pairs, dynamic pos), 3 (routes) partially resolved under "Reduced-mode" subsection |

---

## 7. Rollback

**Full revert, no code edits:**

```python
# src/orchestrator/reduced_live_mode.py
REDUCED_LIVE_MODE: bool = False
```

On next orchestrator restart:

- `TRADING_PAIRS` and `ACTIVE_TRADING_PAIRS` are both the full 8 pairs.
- Cascade levels `15m_fast` and `aligned_trend` fire again.
- `AdaptiveTrend` and `BreakoutTrader` routes re-activate.
- `cross_asset_consensus.compute()` runs and applies ±10 pt adjustments.
- `_get_effective_max_positions` can return `base + 1` under GREEN + confidence ≥ 60 + balance ≥ $60.

No git revert is required; no test changes break.

**Partial rollback** (restore one flag at a time): flip only the specific `ALLOW_*` constant inside `reduced_live_mode.py`. Every helper is independent.

---

## 8. Verification commands

### 8.1 Flag state

```bash
.venv/bin/python -c "
from src.orchestrator import reduced_live_mode as rlm
print('REDUCED_LIVE_MODE:', rlm.REDUCED_LIVE_MODE)
print('ALLOWED_SYMBOLS: ', sorted(rlm.ALLOWED_SYMBOLS))
print('ALLOW_4H_FLIP:                ', rlm.ALLOW_4H_FLIP)
print('ALLOW_1H_CONTINUATION:        ', rlm.ALLOW_1H_CONTINUATION)
print('ALLOW_15M_FAST:               ', rlm.ALLOW_15M_FAST)
print('ALLOW_ALIGNED_TREND:          ', rlm.ALLOW_ALIGNED_TREND)
print('ALLOW_ADAPTIVE_TREND_ROUTE:   ', rlm.ALLOW_ADAPTIVE_TREND_ROUTE)
print('ALLOW_BREAKOUT_TRADER_ROUTE:  ', rlm.ALLOW_BREAKOUT_TRADER_ROUTE)
print('ALLOW_CONSENSUS_ADJUST:       ', rlm.ALLOW_CONSENSUS_ADJUST)
print('ALLOW_DYNAMIC_POS_OVERRIDE:   ', rlm.ALLOW_DYNAMIC_POS_OVERRIDE)
"
```

Expected:

```
REDUCED_LIVE_MODE: True
ALLOWED_SYMBOLS:  ['SOL/USDT:USDT', 'SUI/USDT:USDT']
ALLOW_4H_FLIP:                 True
ALLOW_1H_CONTINUATION:         True
ALLOW_15M_FAST:                False
ALLOW_ALIGNED_TREND:           False
ALLOW_ADAPTIVE_TREND_ROUTE:    False
ALLOW_BREAKOUT_TRADER_ROUTE:   False
ALLOW_CONSENSUS_ADJUST:        False
ALLOW_DYNAMIC_POS_OVERRIDE:    False
```

### 8.2 Orchestrator live universe

```bash
.venv/bin/python -c "
from src.orchestrator.main import TRADING_PAIRS, ACTIVE_TRADING_PAIRS
print('TRADING_PAIRS (full universe, for reconciliation):')
for p in TRADING_PAIRS: print('  ', p)
print('ACTIVE_TRADING_PAIRS (narrowed, for signals):')
for p in ACTIVE_TRADING_PAIRS: print('  ', p)
"
```

Expected: `ACTIVE_TRADING_PAIRS` prints exactly `SOL/USDT:USDT` and `SUI/USDT:USDT`.

### 8.3 Targeted tests

```bash
.venv/bin/python -m pytest tests/test_integration/test_reduced_live_mode.py -v
```

Expected: **20 passed**.

### 8.4 Full suite

```bash
.venv/bin/python -m pytest tests/ 2>&1 | tail -3
```

Expected: **833 passed, 3 warnings**.

### 8.5 Post-restart log sanity (after starting the bot)

In `user_data/logs/bot.log`:

- One line per cycle: `subscribing to 4H kline close` appears exactly twice (SOL, SUI) — never for ETH/DOGE/XRP/LINK/AVAX/ADA.
- No `Regime RANGING ... -> AdaptiveTrend` lines (gated).
- No `Regime VOLATILE ... -> BreakoutTrader` lines (gated).
- No `consensus adjustment` lines (skipped).
- No `DYNAMIC POSITION LIMIT:` lines (override disabled).

---

## 9. Known limitations

1. **Positions held on disabled pairs** are **not force-closed**. They continue to be managed by reconciliation, trailing stops, time exit, and SL/TP — just no NEW entries on those pairs. This is intentional: a human can manually close them if desired, but the bot does not force liquidation on a config change.
2. **Reverse-signal exit for disabled pairs** will not fire, because signal generation skips those pairs. An existing ETH LONG will not be force-closed by a newly generated ETH SHORT signal — there is no ETH SHORT signal being generated. Trailing stops / SL / TP / time-exit still protect these positions.
3. **Rollback is by restart only**. Flipping `REDUCED_LIVE_MODE` at runtime has no effect — the value is read at module import. This is deliberate to keep the path auditable.
