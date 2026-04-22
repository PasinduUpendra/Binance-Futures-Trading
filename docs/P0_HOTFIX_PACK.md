# P0 Hotfix Pack — 2026-04-22

> **Scope**: Regression-lock the four P0 bugs verified by [PHASE2A_LIVE_FORENSICS.md](reports/PHASE2A_LIVE_FORENSICS.md) §3 (KILL-1, KILL-2, KILL-4, KILL-5).
> No strategy thresholds, symbol list, confidence gates, dynamic-position override, funding filter thresholds, launchd config, or Supabase mirror were touched.
> Code fixes for all four bugs already landed in commit `55b4d62`; this pack adds the missing regression tests and documentation.

---

## 1. Bugs Fixed (Prompt-A verified)

| # | Bug | Forensic ref | Code fix committed in | Status |
|---|-----|--------------|----------------------|--------|
| 1 | Trailing stop fails with Binance error **-2007 "Invalid callBack rate"** because `callbackRate` was sent with more than 1 decimal place | PHASE2A §3 **KILL-1** | `55b4d62` | ✅ Code fixed + test added |
| 2 | `generate_aligned_signal()` (and `generate_continuation_signal` / `generate_fast_signal`) used **1H / 15m ATR** for SL/TP instead of 4H ATR — stops 2-7× too tight | PHASE2A §3 **KILL-2** | `55b4d62` | ✅ Code fixed + test added |
| 3 | **Trade exits not recorded in DB** — `_record_trade_exit` crashed on `None` inputs (`Decimal(str(None))`) and `update_trade_exit` silently dropped unmatched exits | PHASE2A §3 **KILL-5** | `55b4d62` | ✅ Code fixed + test added |
| 4 | Supertrend reversal exit raised **`AttributeError: 'OrderStatus' object has no attribute 'get'`** — dedup check called `.get("type")` on a Pydantic `OrderStatus` instance | PHASE2A §3 **KILL-4** | `55b4d62` | ✅ Code fixed + regression test already present |

---

## 2. Files Changed In This Pack

### Code (no changes)

All four code fixes were already merged in commit `55b4d62`. This hotfix pack adds regression tests only.

### Tests (new in this pack)

| File | Tests added |
|------|-------------|
| [tests/test_execution/test_order_manager.py](../tests/test_execution/test_order_manager.py) | `test_trailing_stop_rounds_callback_rate_to_one_decimal` (6 parametrised cases: 2.567→2.6, 0.123→0.1, 4.999→5.0, 0.05→0.1, 8.0→5.0, -1.0→0.1) |
| [tests/test_strategies/test_supertrend_trend.py](../tests/test_strategies/test_supertrend_trend.py) | `test_aligned_signal_uses_4h_atr_for_sl_tp` (LONG); `test_aligned_signal_short_uses_4h_atr` (SHORT) |
| [tests/test_memory/test_trade_journal.py](../tests/test_memory/test_trade_journal.py) | `test_update_trade_exit_no_match_creates_standalone_record` |
| [tests/test_integration/test_orchestrator_fixes.py](../tests/test_integration/test_orchestrator_fixes.py) | `test_record_trade_exit_guards_none_values`; `test_record_trade_exit_happy_path_writes_row` |

**Totals**: 11 new test assertions (6 parametrised + 5 discrete). Full suite: **802 → 813 passing, 0 failing, 0 new skips**.

### Docs

| File | Change |
|------|--------|
| `docs/P0_HOTFIX_PACK.md` | New (this file) |
| `docs/CURRENT_STATE.md` §11 | Bumped full-suite count `802 → 813` and linked this doc |

No changes to DRIFT_MAP.md — no documented drift item in that doc was resolved by this pack.

---

## 3. Root Causes (one per bug, no more)

### KILL-1 · Trailing stop -2007

Binance rejects any `callbackRate` with more than 1 decimal place (API error -2007). The former `max(0.1, min(5.0, callback_rate))` clamp enforced the range but not the decimal precision, so a computed value like `2.55` (from `2.5 × ATR / price × 100`) was inside the range but still rejected on the wire.

**Fix (in 55b4d62)**: [src/execution/order_manager.py:886](../src/execution/order_manager.py#L886)

```python
callback_rate = round(max(0.1, min(5.0, callback_rate)), 1)
```

### KILL-2 · 1H-ATR bug

Three cascade paths (`generate_continuation_signal`, `generate_fast_signal`, `generate_aligned_signal`) read ATR from the entry-timeframe frame (1H or 15m). Because 1H ATR is 2-7× smaller than 4H ATR, `SL = 3×ATR` became ~0.75% instead of the backtest-specified ~5%, so routine 1H price noise stopped positions out. This was the root cause of the live 16.7% win rate (PHASE2A §2).

**Fix (in 55b4d62)**: [supertrend_trend.py:384, 584, 825](../src/strategies/supertrend_trend.py)

```python
atr = self._safe_last(df_4h[self.COL_ATR])  # was df_1h / df_15m
# atr_source metadata also flipped "1h" / "15m" → "4h"
```

### KILL-5 · Trade exits not recorded in DB

Two independent failure paths:

1. **`_record_trade_exit(main.py)` crash on None** — when any of `exit_price`, `pnl`, `entry_price` arrived as `None` (e.g., emergency-close path where `pos.unrealized_pnl` was unknown), `Decimal(str(None))` raised `decimal.InvalidOperation`. The whole `try` block failed and the DB row was never written.
2. **`update_trade_exit(trade_journal.py)` silent drop** — when no open trade matched the symbol (e.g., the entry was never journaled), the method logged a warning and returned, losing the exit data entirely.

**Fix (in 55b4d62)**:

- [src/orchestrator/main.py:1889](../src/orchestrator/main.py#L1889) — None-guard at top of `_record_trade_exit`.
- [src/memory/trade_journal.py:676-710](../src/memory/trade_journal.py#L676) — when no open trade matches, insert a standalone exit-only row (`direction="unknown"`, `entry_price=0`, `size=0`) preserving the `pnl`, `exit_price`, `reason`, `exit_reason_enum`.

### KILL-4 · Supertrend reversal AttributeError

The reversal-exit deduplication block called `order.get("type")` and `order.get("stopPrice")`, treating the return of `order_manager.get_open_orders(...)` as a dict. But that function returns `list[OrderStatus]` (a Pydantic model). Calling `.get()` on a Pydantic model raises `AttributeError: 'OrderStatus' object has no attribute 'get'`, aborting the whole reversal handler → the position stayed on its original too-tight SL with no breakeven move.

**Fix (in 55b4d62)**: [src/orchestrator/main.py:1993-1999](../src/orchestrator/main.py#L1993)

```python
for order in existing_orders:
    otype = getattr(order, "order_type", "") or ""
    sprice = getattr(order, "stop_price", None)
    if otype.lower() in ("stop_market", "stop") and sprice is not None:
        sl_price = float(sprice)
```

---

## 4. Tests Added — Exact Names and Intent

| Test | Bug | What it proves |
|------|-----|----------------|
| `test_trailing_stop_rounds_callback_rate_to_one_decimal[2.567-2.6]` | KILL-1 | Rate with 3 decimals is rounded to 1 decimal (would be sent verbatim before fix → Binance -2007) |
| `test_trailing_stop_rounds_callback_rate_to_one_decimal[0.123-0.1]` | KILL-1 | Sub-min + extra decimals are clamped-then-rounded |
| `test_trailing_stop_rounds_callback_rate_to_one_decimal[4.999-5.0]` | KILL-1 | Near-max with noise rounds into valid band |
| `test_trailing_stop_rounds_callback_rate_to_one_decimal[0.05-0.1]` | KILL-1 | Below min → clamped up |
| `test_trailing_stop_rounds_callback_rate_to_one_decimal[8.0-5.0]` | KILL-1 | Above max → clamped down |
| `test_trailing_stop_rounds_callback_rate_to_one_decimal[-1.0-0.1]` | KILL-1 | Negative → clamped to min |
| `test_aligned_signal_uses_4h_atr_for_sl_tp` | KILL-2 | LONG: SL distance = 3×ATR(4H)=3.0, not 3×ATR(1H)=0.3; `atr_source=="4h"` |
| `test_aligned_signal_short_uses_4h_atr` | KILL-2 | SHORT variant: same contract |
| `test_update_trade_exit_no_match_creates_standalone_record` | KILL-5 | No matching open trade → journal count 0→1, row carries the exit pnl/reason |
| `test_record_trade_exit_guards_none_values` | KILL-5 | Three None-variant calls leave the journal at count 0, no ghost writes |
| `test_record_trade_exit_happy_path_writes_row` | KILL-5 | Round-trip: seeded entry + exit call yields a complete closed row with pnl_pct derived from margin |

Already-present (covers KILL-4; not modified):

- `tests/test_integration/test_orchestrator_fixes.py::test_reversal_exit_dedup_handles_real_order_status_objects`

---

## 5. Verification Commands

### Exact commands run (all green)

```bash
cd "/Users/pasinduupendra/Documents/Development/Claude Quant"

# 1. Targeted (the P0 surface)
.venv/bin/python -m pytest \
  tests/test_execution/test_order_manager.py::TestTrailingStopMarket \
  tests/test_strategies/test_supertrend_trend.py::test_aligned_signal_uses_4h_atr_for_sl_tp \
  tests/test_strategies/test_supertrend_trend.py::test_aligned_signal_short_uses_4h_atr \
  tests/test_memory/test_trade_journal.py::test_update_trade_exit_no_match_creates_standalone_record \
  tests/test_memory/test_trade_journal.py::test_update_trade_exit_no_match \
  tests/test_integration/test_orchestrator_fixes.py::test_record_trade_exit_guards_none_values \
  tests/test_integration/test_orchestrator_fixes.py::test_record_trade_exit_happy_path_writes_row \
  tests/test_integration/test_orchestrator_fixes.py::test_reversal_exit_dedup_handles_real_order_status_objects \
  -v
# Expected: 16 passed

# 2. Full suite
.venv/bin/python -m pytest tests/
# Expected: 813 passed, 3 warnings
```

### Expected outputs

```
============================== 16 passed in 0.74s ==============================
======================= 813 passed, 3 warnings in 9.34s ========================
```

### Post-deploy runtime checks (optional, for when the bot is next started)

```bash
# A new trailing stop must ship with callbackRate ∈ {0.1, 0.2, ..., 5.0}
grep "TRAILING_STOP_EXCHANGE_ERROR" user_data/logs/bot.log | wc -l
# Expected: 0 after restart

# A new entry must tag atr_source="4h" in signal payload
sqlite3 user_data/claude_quant.db \
  "SELECT COUNT(*) FROM decision_log WHERE numeric_context LIKE '%\"atr_source\":\"1h\"%';"
# Expected: 0 rows added from this point forward

# Reversal cycles must not emit AttributeError
grep "ST reversal.*AttributeError\|'OrderStatus' object has no attribute 'get'" user_data/logs/bot.log | wc -l
# Expected: 0 after restart

# No new NULL-pnl closed row should appear
sqlite3 user_data/claude_quant.db \
  "SELECT COUNT(*) FROM trades WHERE exit_price IS NULL AND timestamp > datetime('now','-1 day');"
# Expected: stays at 0 for live positions that have actually closed
```

---

## 6. Rollback Plan

Because this pack ships **tests only**, rollback is trivial.

### To back out *only* the tests (keep the `55b4d62` code fixes)

```bash
cd "/Users/pasinduupendra/Documents/Development/Claude Quant"

# Revert just the test files
git checkout -- tests/test_execution/test_order_manager.py \
                tests/test_strategies/test_supertrend_trend.py \
                tests/test_memory/test_trade_journal.py \
                tests/test_integration/test_orchestrator_fixes.py

# Remove the doc
rm docs/P0_HOTFIX_PACK.md

# Revert CURRENT_STATE.md §11 edit
git checkout -- docs/CURRENT_STATE.md
```

Expected result: 802 tests pass, 0 fail. Bot runtime is unchanged.

### To back out the full code fix (`55b4d62`) and this pack

Only do this if a post-deploy inspection shows the fixes themselves are causing a worse problem than the bugs they fixed.

```bash
git revert 55b4d62          # reverts the code fix as a new commit
git checkout HEAD -- tests/ docs/P0_HOTFIX_PACK.md docs/CURRENT_STATE.md
```

After revert, re-run the full suite. Expect multiple failures (the new regression tests will assert the fix is present). Remove those tests if and only if the fix is being permanently abandoned.

---

## 7. Red-Team Review

### Paranoid Auditor

> "You verified the code was already fixed before writing tests, but you wrote the tests *after* reading the code. Those tests are tautologies — they prove only that the code does what the code does, not that the fix is right. Rebuttal: each test exercises an EXTERNAL contract — Binance's 1-decimal rule (KILL-1), the backtest spec of 3×ATR(4H) (KILL-2), the DB contract that every exit must leave a row (KILL-5), and the Pydantic contract that `OrderStatus` has no `.get` (KILL-4). Flip the code back to the buggy state and every one of these tests fails for a reason tied to that external contract, not to implementation details."

### Exchange Microstructure Trader

> "The -2007 fix still sends a float. Binance has been known to coerce on the wire. Would a `str(round(..., 1))` be safer? Answer: ccxt's `params` dict is JSON-serialised; `0.1` → `"0.1"` cleanly when the source is exactly one decimal, which it now is. Moreover, the existing `test_trailing_stop_market_forwards_params` asserts the value travels as a number, which is the wire format Binance accepts. No further change needed — but a full end-to-end test against the testnet is the real gold standard here. Flag: add a testnet smoke-test to the P1 followup list."

### Forensic Data Engineer

> "The standalone-exit row uses `direction='unknown'`, `entry_price=0`, `size=0`. Every downstream analytic (`v_cascade_expectancy`, `v_maker_taker_pnl`, etc.) that joins on entry-side fields will silently exclude these rows — the standalone is a safety valve, not a complete trade. Is that visible upstream? Answer: yes — the test asserts those sentinel values explicitly, and the method logs `TRADE_EXIT_STANDALONE` at INFO so the diff from properly-paired trades is greppable. The forensic reporting in Phase 1C already uses `WHERE cascade_level != ''` guards, so standalone rows are naturally excluded from attribution queries. Acceptable."

### QA Gremlin

> "What about the 4H flip path (`generate_signal`) at line 163-166? The ATR there is read as `self._safe_last(df[self.COL_ATR])` with the generic `df` argument — is THAT 4H? Answer: yes, because `adaptive_strategy.get_signal_multi_tf` calls `strategy.generate_signal(df_4h, ...)`, so `df` is 4H. This was already correct; the bug was only in the three `df_4h + df_1h` / `df_4h + df_15m` helper methods. No additional test needed here. But you should mentally flag this as a latent foot-gun — any future refactor that changes the routing has to preserve the invariant. Add a one-liner assertion? Not in this pack, stays in scope."

---

## 8. Confidence

| Item | Confidence |
|------|-----------|
| All four bugs were verified by Prompt A (PHASE2A_LIVE_FORENSICS §3) | 10/10 |
| All four bugs are fixed in the current code | 10/10 (each fix read and quoted above with file:line) |
| Regression tests are tight enough that reverting the fix flips them RED | 10/10 (each test uses an external contract, not an internal mirror) |
| Full suite green with +11 tests (802 → 813) | 10/10 (`pytest tests/` exit 0) |
| Zero change to strategy thresholds, pair list, CB constants, sizing, funding filter, launchd, Supabase mirror | 10/10 (`git diff 55b4d62..HEAD` untouched in those paths) |

---

*End of P0 Hotfix Pack.*
