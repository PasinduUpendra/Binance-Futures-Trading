# Phase 1B Trade Attribution — Implementation Reference

> Status: **COMPLETE** · Deployed: 2026-04  
> Spec: `docs/LIVE_FORENSICS_SPEC.md §2.2 – §2.4`

---

## A. Schema Changes

### A.1 — 14 new columns added to `trades` table

| Column | Type | Populated at | Default | Notes |
|--------|------|-------------|---------|-------|
| `cascade_level` | TEXT | entry | `''` | e.g. `"4h_flip"`, `"aligned"` |
| `confidence_bucket` | TEXT | entry | `''` | `"85+"` · `"70-85"` · `"55-70"` · `"45-55"` |
| `regime_at_entry` | TEXT | entry | `''` | regime enum value at the moment of execution |
| `atr_at_entry` | TEXT | entry | `'0'` | 4H ATR in quote currency, stored as decimal string |
| `entry_slippage_bps` | REAL | entry | `0` | `(fill_price − signal_price) / signal_price × 10 000` · positive = paid more |
| `exit_slippage_bps` | REAL | exit | `0` | `0.0` in Phase 1B (market exit, not tracked) |
| `maker_entry` | INTEGER | entry | `0` | `1` if filled via post-only limit, `0` = taker market |
| `maker_exit` | INTEGER | exit | `0` | always `0` in Phase 1B (all closes are market orders) |
| `fees_usd` | TEXT | entry | `'0'` | exchange-reported fee for entry leg (best-effort; may be `'0'` if Binance omits it) |
| `funding_usd` | TEXT | — | `'0'` | **Phase 1B: always `'0'`** — requires funding history API (Phase 2) |
| `hold_bars` | INTEGER | exit | `0` | duration in 1H bars ≈ `int(duration_hours)` |
| `exit_reason_enum` | TEXT | exit | `''` | canonical exit type (see §A.3) |
| `consensus_adj` | REAL | entry | `0` | cross-asset consensus adjustment applied to signal confidence |
| `funding_adj` | REAL | entry | `0` | funding rate confidence adjustment |

### A.2 — New `fill_events` table (DDL)

```sql
CREATE TABLE IF NOT EXISTS fill_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT    NOT NULL,
    side_of_trade       TEXT    NOT NULL,   -- 'entry' | 'exit'
    timestamp_utc       TEXT    NOT NULL,
    order_type          TEXT    NOT NULL,   -- 'limit_gtx' | 'market' | 'stop_market' | 'tp_market' | 'native_trail'
    requested_price     TEXT    NOT NULL,
    fill_price          TEXT    NOT NULL,
    filled_qty          TEXT    NOT NULL,
    is_maker            INTEGER NOT NULL,
    fees_usd            TEXT    NOT NULL,
    client_order_id     TEXT,
    exchange_order_id   TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_fill_events_trade
    ON fill_events (trade_id);
```

### A.3 — `exit_reason_enum` canonical values

| Raw reason string | `exit_reason_enum` |
|-------------------|--------------------|
| `"trailing_stop"` | `"trail"` |
| `"time_exit"` | `"time_exit"` |
| `"reversal_exit"` | `"reversal"` |
| `"wrong_side_force_close"` | `"wrong_side_force"` |
| `"swap"` | `"swap"` |
| `"manual"` | `"manual"` |
| `"sl_hit"` | `"sl_hit"` |
| `"tp_hit"` | `"tp_hit"` |
| *(any other value)* | *(stored as-is)* |

---

## B. Files Changed

| File | What changed |
|------|-------------|
| [src/data/database.py](../src/data/database.py) | `_SCHEMA_TRADES` extended (14 cols); `_SCHEMA_FILL_EVENTS` added; `_INDEXES` updated; `_initialize()` includes fill_events; `_run_migrations()` idempotently adds all 14 cols + creates fill_events; new methods: `insert_fill_event()`, `get_fill_events_for_trade()`, `update_trade_attribution()` |
| [src/memory/trade_journal.py](../src/memory/trade_journal.py) | `TradeEntry` gains 14 new optional fields; `_CREATE_TABLE` / `_INSERT_TRADE` / `_trade_to_params()` updated; `_row_to_trade_entry()` backward-safe (`.get()` defaults); `_initialize_db()` migration loops over all new cols; `record_trade_entry()` now returns `str` (trade_id); `update_trade_exit()` new signature returns `str \| None` |
| [src/orchestrator/main.py](../src/orchestrator/main.py) | Module-level helpers `_compute_confidence_bucket()` + `_exit_reason_to_enum()`; `_execute_signal()` gains `consensus_adj: float = 0.0` param; entry attribution block + fill_events insert; decision_logger Phase 1B stages: `post_only_attempt`, `market_fallback`, `sl_place`, `tp_place`, `position_open_recorded`; `_record_trade_exit()` extended with `hold_bars`, `exit_*` params + fill_events exit insert; exit callers (`_manage_trailing_stops`, `_check_time_based_exits`) pass `hold_bars` and `exit_filled_qty` |
| [tests/test_data/test_phase1b_attribution.py](../tests/test_data/test_phase1b_attribution.py) | 20 new tests covering schema, migration, model, record/update, fill_events, attribution, backward compat, full pipeline |
| [tests/test_memory/test_trade_journal.py](../tests/test_memory/test_trade_journal.py) | Updated 3 assertions to match `str \| None` return type of `update_trade_exit()` |

---

## C. Fields Populated — Entry vs Exit

### At entry (inside `_execute_signal()`)

| Field | Source |
|-------|--------|
| `cascade_level` | `getattr(signal, "cascade_level", "") or ""` |
| `confidence_bucket` | `_compute_confidence_bucket(signal.confidence)` |
| `regime_at_entry` | `regime.regime.value` if available, else `signal.regime` |
| `atr_at_entry` | `df_4h["atr"].dropna().iloc[-1]` (same value as trailing-stop `atr_4h`) |
| `entry_slippage_bps` | `direction_sign × (fill − signal_price) / signal_price × 10_000` |
| `maker_entry` | `1` if `filled_via == "maker"`, else `0` |
| `fees_usd` | `getattr(order_result, "fee", None) or "0"` |
| `consensus_adj` | `consensus_adj` parameter passed from `_run_cycle` |
| `funding_adj` | `fr_result.confidence_adjustment` if available |

### At exit (inside `_record_trade_exit()`)

| Field | Source |
|-------|--------|
| `hold_bars` | caller-provided `hold_bars` param; fallback: `int(duration_hours)` |
| `exit_reason_enum` | `_exit_reason_to_enum(reason)` |
| `exit_slippage_bps` | always `0.0` in Phase 1B |
| `maker_exit` | always `0` in Phase 1B |

### fill_events rows

| Event | `side_of_trade` | `order_type` | `is_maker` |
|-------|----------------|-------------|----------|
| Entry via limit GTX | `entry` | `limit_gtx` | `1` |
| Entry via market | `entry` | `market` | `0` |
| Exit (all closures) | `exit` | `market` | `0` |

---

## D. Remaining Missing / Incomplete Fields (Phase 1B Limitations)

| Field | Status | Phase 2 plan |
|-------|--------|-------------|
| `funding_usd` | **Always `'0'`** — Binance funding history requires a separate API call not integrated in current architecture | Query `GET /fapi/v1/income?incomeType=FUNDING_FEE` post-exit |
| `exit_slippage_bps` | Always `0.0` — exit order results are not returned to `_record_trade_exit` in the current architecture | Wire exit `OrderResult` through all close paths |
| `maker_exit` | Always `0` — close orders are always market/stop-market | Change to `1` when a limit GTC close is used |
| `fees_usd` (entry) | Best-effort — Binance sometimes returns `fee=0` in the create response; actual fee visible via trade history | Backfill from `/fapi/v1/userTrades` |
| fill_events `fees_usd` (exit leg) | Always `"0"` in Phase 1B | Wire exit fill details from position reconciliation |
| `atr_at_entry` for fast/4h-flip entries | 4H `df_4h["atr"]` used everywhere — correct for `generate_aligned_signal` path; fast entries use 15m signals but log 4H ATR for consistency | Log both 4H and 1H ATR values |

---

## E. Verification SQL

```sql
-- 1. All 14 attribution columns are present
SELECT count(*) AS cols_present
FROM pragma_table_info('trades')
WHERE name IN (
    'cascade_level','confidence_bucket','regime_at_entry',
    'atr_at_entry','entry_slippage_bps','exit_slippage_bps',
    'maker_entry','maker_exit','fees_usd','funding_usd',
    'hold_bars','exit_reason_enum','consensus_adj','funding_adj'
);
-- Expected: 14

-- 2. fill_events table and index exist
SELECT name FROM sqlite_master
WHERE type IN ('table','index')
  AND name IN ('fill_events','idx_fill_events_trade')
ORDER BY name;
-- Expected: 2 rows

-- 3. Recent 10 trades with attribution
SELECT
    trade_id,
    symbol,
    direction,
    confidence_bucket,
    regime_at_entry,
    entry_slippage_bps,
    maker_entry,
    hold_bars,
    exit_reason_enum,
    consensus_adj
FROM trades
ORDER BY timestamp DESC
LIMIT 10;

-- 4. Distribution of confidence buckets
SELECT confidence_bucket, count(*) AS n
FROM trades
WHERE confidence_bucket != ''
GROUP BY confidence_bucket
ORDER BY n DESC;

-- 5. fill_events paired with their trades
SELECT
    t.symbol,
    t.direction,
    fe.side_of_trade,
    fe.order_type,
    fe.requested_price,
    fe.fill_price,
    fe.is_maker,
    fe.fees_usd,
    fe.timestamp_utc
FROM fill_events fe
JOIN trades t ON fe.trade_id = t.trade_id
ORDER BY fe.id DESC
LIMIT 20;

-- 6. Maker vs taker entry rate
SELECT
    maker_entry,
    count(*) AS trades,
    round(avg(entry_slippage_bps), 2) AS avg_slippage_bps
FROM trades
WHERE entry_slippage_bps != 0
GROUP BY maker_entry;

-- 7. Trades missing fill_events (data quality check)
SELECT t.trade_id, t.symbol, t.timestamp
FROM trades t
LEFT JOIN fill_events fe ON fe.trade_id = t.trade_id
WHERE fe.id IS NULL
  AND t.timestamp > date('now', '-7 days')
ORDER BY t.timestamp DESC;

-- 8. Exit reason distribution
SELECT exit_reason_enum, count(*) AS n
FROM trades
WHERE exit_reason_enum != ''
GROUP BY exit_reason_enum
ORDER BY n DESC;

-- 9. Average hold_bars by exit reason
SELECT exit_reason_enum, round(avg(hold_bars), 1) AS avg_hold_bars
FROM trades
WHERE hold_bars > 0
GROUP BY exit_reason_enum;
```

---

## F. Migration Safety

Both `DatabaseManager._run_migrations()` and `TradeJournal._initialize_db()` are **idempotent**:

- Each column is added only if `PRAGMA table_info(trades)` does not already contain it.
- `CREATE TABLE IF NOT EXISTS fill_events` is safe to run on any existing DB.
- No data is modified; only schema is extended.
- Tests `test_migration_adds_attribution_columns_to_old_db` and `test_trade_journal_migration_adds_columns_to_old_db` verify this against a pre-existing DB.
