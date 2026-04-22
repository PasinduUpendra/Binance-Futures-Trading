# Phase 1A — Decision-Funnel Logging

Implementation of the decision-log foundation from `LIVE_FORENSICS_SPEC.md` Phase 1A.
Zero changes to strategy thresholds, pair lists, circuit-breaker constants, or execution behaviour.

---

## A. Schema Added

### `decision_log` table

```sql
CREATE TABLE IF NOT EXISTS decision_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id          INTEGER NOT NULL,
    timestamp_utc     TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    stage             TEXT NOT NULL,
    outcome           TEXT NOT NULL,          -- pass | reject | skip | error
    reason            TEXT,
    numeric_context   TEXT,                   -- JSON blob
    cascade_level     TEXT,
    confidence        REAL,
    regime            TEXT,
    FOREIGN KEY (cycle_id) REFERENCES cycle_history(id)
);
```

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_decision_log_cycle      ON decision_log(cycle_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_symbol_stage ON decision_log(symbol, stage);
CREATE INDEX IF NOT EXISTS idx_decision_log_timestamp  ON decision_log(timestamp_utc);
```

### `cycle_funnel` view

```sql
CREATE VIEW IF NOT EXISTS cycle_funnel AS
SELECT cycle_id, stage, COUNT(*) AS attempts,
    SUM(CASE WHEN outcome='pass'   THEN 1 ELSE 0 END) AS passes,
    SUM(CASE WHEN outcome='reject' THEN 1 ELSE 0 END) AS rejects,
    SUM(CASE WHEN outcome='error'  THEN 1 ELSE 0 END) AS errors
FROM decision_log GROUP BY cycle_id, stage;
```

---

## B. Files Changed

| File | Change |
|------|--------|
| `src/data/database.py` | Added `_SCHEMA_DECISION_LOG`, `_VIEW_CYCLE_FUNNEL`, 3 indexes; updated `_initialize()`; added `begin_cycle`, `finish_cycle`, `insert_decision_log`, `get_decision_logs`; `insert_decision_log` now accepts `dict\|str` for `numeric_context` |
| `src/memory/decision_logger.py` | **New file.** `VALID_STAGES` (23 stages incl. 6 Phase-1B stubs), `VALID_OUTCOMES`, `DecisionLogger` class |
| `src/memory/__init__.py` | Added `DecisionLogger` to public exports and `__all__` |
| `src/orchestrator/main.py` | `DecisionLogger` import; `self._current_cycle_id`; `self.decision_logger`; `begin_cycle` at cycle start; 17 stage hooks; `_execute_signal` signature `cycle_id: int = 0`; `_save_cycle_state` → `finish_cycle` |
| `tests/test_data/test_decision_log.py` | **New file.** 18 tests — table/view/index creation, insert/retrieve, begin/finish cycle, FK, funnel view |
| `tests/test_memory/test_decision_logger.py` | **New file.** 18 tests — all outcomes, guard clauses, exception swallowing, JSON serialization |
| `tests/test_integration/test_decision_log_orchestrator.py` | **New file.** 10 tests — data-fetch rows, execute-signal stage rows, FK integrity after finish_cycle |

---

## C. Stages Wired (17 total)

All hooks are in `src/orchestrator/main.py`.

### Step 1b — Data Fetch Loop (`_run_cycle`)

| Stage | Outcomes logged | Location |
|-------|-----------------|----------|
| `data_fetch_4h` | pass (rows count), reject (insufficient_rows / validation_failed), error (exception) | Step 1b `for pair in TRADING_PAIRS` loop |
| `data_fetch_1h` | pass, reject (insufficient_rows / validation_failed) | same loop |
| `data_fetch_15m` | pass, reject (validation_failed), skip (insufficient_rows) | same loop |

### Step 3 — Signal Generation Loop (`_run_cycle`)

| Stage | Outcomes logged | Notes |
|-------|-----------------|-------|
| `regime_detect` | pass (with adx + regime), error (exception) | called before `get_signal_multi_tf` so it runs even when signal is None |
| `signal_generate` | pass (confidence, cascade_level, direction), reject (no_signal) | |
| `confidence_gate` | pass (confidence, min_confidence=45) | gate is inside `adaptive_strategy`; always pass here |
| `position_overlap_skip` | skip (existing_side, signal_direction) | only when same-direction position held |
| `cross_asset_consensus_adjust` | pass (adjustment, final_confidence) | always pass; adj may be 0.0 |

### Steps 4-7 — `_execute_signal`

| Stage | Outcomes logged | Notes |
|-------|-----------------|-------|
| `funding_filter` | pass, reject, skip (fetch_failed — non-blocking) | `FundingRateFilter.evaluate` |
| `leverage_determine` | pass, reject (leverage==0) | `LeverageManager.determine_leverage` |
| `volatility_adjust` | pass (always; records before/after leverage) | `VolatilityModel.adjust_leverage` |
| `sizing` | pass, reject (balance_below_5) | margin calculation |
| `min_notional` | pass, reject (below_pair_minimum) | per-pair `MIN_NOTIONAL` map |
| `liquidation_buffer` | pass, reject (buffer_below_5pct) | `LeverageManager.calculate_liquidation_buffer` |
| `price_validate` | pass, reject, error | `PriceValidator.validate_price` — error = exception in validator |
| `signal_validate` | pass, reject, error | `SignalValidator.validate_signal` — error = exception in validator |
| `decision_audit` | pass, reject, skip | `DecisionAuditor.audit_decision` — logged before `return None` |

---

## D. Verification Commands

Run against the live DB at `user_data/claude_quant.db`:

```bash
# 1. Confirm table + view exist
sqlite3 user_data/claude_quant.db ".tables" | tr ' ' '\n' | grep -E "decision_log|cycle_funnel"

# 2. Count rows per stage for the last cycle
sqlite3 user_data/claude_quant.db \
  "SELECT stage, outcome, COUNT(*) FROM decision_log WHERE cycle_id=(SELECT MAX(id) FROM cycle_history) GROUP BY stage, outcome;"

# 3. Funnel view for last cycle
sqlite3 user_data/claude_quant.db \
  "SELECT * FROM cycle_funnel WHERE cycle_id=(SELECT MAX(id) FROM cycle_history) ORDER BY stage;"

# 4. Confirm no orphaned PENDING rows (should be 0 for healthy runs)
sqlite3 user_data/claude_quant.db \
  "SELECT COUNT(*) FROM cycle_history WHERE circuit_breaker_level='PENDING';"

# 5. FK check
sqlite3 user_data/claude_quant.db "PRAGMA foreign_key_check(decision_log);"

# 6. Check last 10 decision rows
sqlite3 user_data/claude_quant.db \
  "SELECT cycle_id, symbol, stage, outcome, reason FROM decision_log ORDER BY id DESC LIMIT 10;"
```

---

## E. Known Gaps (Phase 1B)

The following `VALID_STAGES` stub entries are registered but **not yet wired**:

| Stage | Phase | Description |
|-------|-------|-------------|
| `post_only_attempt` | 1B | Maker-first limit order attempt |
| `market_fallback` | 1B | Taker market order fallback |
| `sl_place` | 1B | Stop-loss order placement |
| `tp_place` | 1B | Take-profit order placement |
| `native_trail_place` | 1B | Trailing stop order placement |
| `position_open_recorded` | 1B | DB write of opened position |

Additional Phase 1B work:
- `fill_events` table (fills, slippage, VWAP vs expected)
- `trades` attribution: add `cycle_id` FK column
- Daily-report SQL that aggregates from `cycle_funnel`
- `_on_4h_close` regime logging (uses `self._current_cycle_id` from last `_run_cycle`; rows are dropped if bot just started with `_current_cycle_id=0`)

---

## F. Red-Team Notes

**Auditor**
- Orphaned `PENDING` rows are identifiable via query E.4 above. They indicate a cycle that crashed before `_save_cycle_state`. Not harmful to integrity.
- `cycle_id=0` guard in `DecisionLogger.log()` ensures no FK violation when `begin_cycle` fails or `_on_4h_close` fires before the first `_run_cycle`.
- All `decision_log` writes happen via the same `_get_conn()` connection as all other DB writes — no concurrency concerns.

**Forensic Data Engineer**
- `numeric_context` is always a JSON object or NULL. The `default=str` serializer prevents `Decimal`/`datetime` from crashing serialization.
- Stage names match the spec exactly. `VALID_STAGES` is the authoritative list; any typo at a call site produces a WARNING and a dropped row (no silent data corruption).
- `cycle_funnel` view aggregates `skip` into neither `passes` nor `rejects` — queries that want skip counts must use `SUM(CASE WHEN outcome='skip' THEN 1 ELSE 0 END)`.

**QA Gremlin**
- If `begin_cycle` raises, `_current_cycle_id` stays 0 and all `decision_logger.log()` calls are silently dropped for that cycle. The cycle still runs normally.
- If `_execute_signal` is called without `cycle_id` (default=0), all stage logs are dropped — safe, not a crash.
- Two concurrent `_execute_signal` calls are prevented by `self._execution_lock`; no TOCTOU on `cycle_id`.
- DB locked: `insert_decision_log` wraps the SQLite write; `DecisionLogger.log()` catches all exceptions — a DB lock produces a WARNING, not an exception into the hot path.
