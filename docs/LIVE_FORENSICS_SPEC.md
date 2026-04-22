# LIVE_FORENSICS_SPEC.md — Evaluate Live Expectancy By Setup

> **Date:** 2026-04-22
> **Goal:** Replace all backtest-based claims with live-verified attribution by setup. This spec is mandatory for [MASTER_ROADMAP.md Phase 1](MASTER_ROADMAP.md).
> **Principle:** If it isn't in the schema, it doesn't exist. Every decision the bot makes and every dollar it wins or loses must be traceable to specific rows.

---

## 1. What We Cannot Answer Today (gap proof)

Queries that CANNOT be answered from current `claude_quant.db`:

| Question | Why it fails |
|---|---|
| "How many signals were generated this week broken down by cascade level?" | Cascade level isn't stored |
| "What % of signals passed the confidence gate but failed the funding filter?" | No stage-by-stage decision log |
| "Of my losing trades, what fraction was fee + funding drag vs raw edge loss?" | Only total PnL stored; no attribution |
| "Did the post-only maker entry get filled, or did we fall back to taker?" | Not recorded |
| "What's the per-pair win rate for signals from the 1H continuation cascade level?" | Not recorded |
| "Which regime produced the worst expectancy over the last 30 trades?" | Regime at entry not stored per trade |
| "How often did liquidation buffer reject a signal?" | No structured rejection log |
| "What was the slippage on entry vs exit on average?" | Not recorded |

Every one of these is a Phase-2 decision gate in [MASTER_ROADMAP.md](MASTER_ROADMAP.md). We cannot make those decisions without these answers.

---

## 2. Required Schema Additions

### 2.1 New table: `decision_log`

One row per (cycle, symbol, stage) — even when the stage passes. The whole point is to see conversion rates.

```sql
CREATE TABLE IF NOT EXISTS decision_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id          INTEGER NOT NULL,
    timestamp_utc     TEXT NOT NULL,        -- ISO8601
    symbol            TEXT NOT NULL,
    stage             TEXT NOT NULL,        -- enum below
    outcome           TEXT NOT NULL,        -- 'pass' | 'reject' | 'skip' | 'error'
    reason            TEXT,                 -- human-readable, brief
    numeric_context   TEXT,                 -- JSON blob, queryable via json_extract
    cascade_level     TEXT,                 -- null unless relevant
    confidence        REAL,                 -- null unless relevant
    regime            TEXT,                 -- null unless relevant
    FOREIGN KEY (cycle_id) REFERENCES cycle_history(id)
);
CREATE INDEX idx_decision_log_cycle ON decision_log(cycle_id);
CREATE INDEX idx_decision_log_symbol_stage ON decision_log(symbol, stage);
CREATE INDEX idx_decision_log_timestamp ON decision_log(timestamp_utc);
```

**Stage enum (write row for EVERY stage, every symbol, every cycle):**

```
data_fetch_4h, data_fetch_1h, data_fetch_15m,
regime_detect,
signal_generate,              -- emits cascade_level on pass
confidence_gate,              -- MIN_CONFIDENCE=45
cross_asset_consensus_adjust, -- records the ±adjustment
position_overlap_skip,        -- same-direction held
funding_filter,
leverage_determine,
volatility_adjust,
sizing,
min_notional,
liquidation_buffer,
price_validate,               -- Layer 2
signal_validate,              -- Layer 3
decision_audit,               -- Layer 4
post_only_attempt,            -- outcome: filled/unfilled
market_fallback,
sl_place,
tp_place,
native_trail_place,
position_open_recorded
```

**`numeric_context` JSON schema (examples):**

```json
// stage=signal_generate
{"cascade_level":"continuation","confidence":67.3,"flip_age_1h":4,"adx":24.1,"atr":1.82}
// stage=liquidation_buffer
{"entry":2450.5,"leverage":6,"direction":"long","buffer_pct":0.147,"mmr":0.004,"is_safe":true}
// stage=post_only_attempt
{"target_price":2450.3,"best_bid":2450.3,"wait_seconds":5,"filled":false}
// stage=funding_filter
{"funding_rate":0.00042,"signal_direction":"long","threshold":0.0005,"contrarian":false,"adjustment":0}
```

**Why JSON blob:** this layer must capture arbitrary context per stage without a schema rev. Keep the HOT columns (cycle_id, symbol, stage, outcome) first-class; everything else via `json_extract`.

### 2.2 Extend `trades` with attribution

```sql
ALTER TABLE trades ADD COLUMN cascade_level        TEXT DEFAULT '';
ALTER TABLE trades ADD COLUMN confidence_bucket    TEXT DEFAULT '';   -- '45-55','55-70','70-85','85+'
ALTER TABLE trades ADD COLUMN regime_at_entry      TEXT DEFAULT '';
ALTER TABLE trades ADD COLUMN atr_at_entry         TEXT DEFAULT '0';  -- Decimal-as-TEXT
ALTER TABLE trades ADD COLUMN entry_slippage_bps   REAL DEFAULT 0;    -- (fill - signal_price) / signal_price * 10000, signed by direction
ALTER TABLE trades ADD COLUMN exit_slippage_bps    REAL DEFAULT 0;
ALTER TABLE trades ADD COLUMN maker_entry          INTEGER DEFAULT 0; -- 0/1
ALTER TABLE trades ADD COLUMN maker_exit           INTEGER DEFAULT 0;
ALTER TABLE trades ADD COLUMN fees_usd             TEXT DEFAULT '0';  -- Decimal
ALTER TABLE trades ADD COLUMN funding_usd          TEXT DEFAULT '0';  -- signed (paid = negative)
ALTER TABLE trades ADD COLUMN hold_bars            INTEGER DEFAULT 0;
ALTER TABLE trades ADD COLUMN exit_reason_enum     TEXT DEFAULT '';   -- 'sl_hit','tp_hit','trail','time_exit','reversal','wrong_side_force','swap','manual'
ALTER TABLE trades ADD COLUMN consensus_adj        REAL DEFAULT 0;    -- applied at entry
ALTER TABLE trades ADD COLUMN funding_adj          REAL DEFAULT 0;    -- applied at entry
```

### 2.3 New table: `fill_events`

For execution-quality attribution.

```sql
CREATE TABLE IF NOT EXISTS fill_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT NOT NULL,
    side_of_trade   TEXT NOT NULL,   -- 'entry' | 'exit'
    timestamp_utc   TEXT NOT NULL,
    order_type      TEXT NOT NULL,   -- 'limit_gtx' | 'market' | 'stop_market' | 'tp_market' | 'native_trail'
    requested_price TEXT NOT NULL,
    fill_price      TEXT NOT NULL,
    filled_qty      TEXT NOT NULL,
    is_maker        INTEGER NOT NULL,
    fees_usd        TEXT NOT NULL,
    client_order_id TEXT,
    exchange_order_id TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);
CREATE INDEX idx_fill_events_trade ON fill_events(trade_id);
```

### 2.4 View: `cycle_funnel`

```sql
CREATE VIEW IF NOT EXISTS cycle_funnel AS
SELECT
    cycle_id,
    stage,
    COUNT(*)                                        AS attempts,
    SUM(CASE WHEN outcome='pass' THEN 1 ELSE 0 END) AS passes,
    SUM(CASE WHEN outcome='reject' THEN 1 ELSE 0 END) AS rejects,
    SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS errors
FROM decision_log
GROUP BY cycle_id, stage;
```

---

## 3. Required Code Hooks

### 3.1 `DecisionLogger` class

`src/memory/decision_logger.py` (new):

```python
class DecisionLogger:
    def __init__(self, db: DatabaseManager): ...
    def log(
        self,
        cycle_id: int,
        symbol: str,
        stage: str,          # see enum
        outcome: str,        # 'pass'|'reject'|'skip'|'error'
        reason: str = "",
        numeric_context: dict | None = None,
        cascade_level: str | None = None,
        confidence: float | None = None,
        regime: str | None = None,
    ) -> None: ...
```

### 3.2 Orchestrator integration

In `_run_cycle` and `_execute_signal`:
- Start of cycle: write a `cycle_history` row FIRST (so cycle_id exists).
- Every stage in the order listed in §2.1 writes a `decision_log` row before its gate.
- `trades` row extended fields populated at position-open.
- `fill_events` row per execution leg.

### 3.3 Position-close attribution

On close (trailing / SL / TP / time-exit / reversal / wrong-side / swap):
- Compute `fees_usd` = maker fees + taker fees accumulated across `fill_events`.
- Compute `funding_usd` = sum of funding payments during hold.
- Compute `exit_slippage_bps` = `(fill_price - mark_price_at_trigger) / mark_price_at_trigger * 10000 * direction_sign`.
- Write `exit_reason_enum`.

---

## 4. The 12 Canonical Queries

These are the forensic-analyst agent's starter pack. Each must return a non-empty result after 7 days of instrumented live.

1. **Cascade-level conversion funnel**
   ```sql
   SELECT stage, cascade_level, outcome, COUNT(*)
   FROM decision_log WHERE stage IN ('signal_generate','confidence_gate','liquidation_buffer','decision_audit')
   GROUP BY stage, cascade_level, outcome;
   ```

2. **Per-cascade expectancy**
   ```sql
   SELECT cascade_level, COUNT(*) n, AVG(CAST(pnl AS REAL)) avg_pnl,
          SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1 ELSE 0 END)*1.0/COUNT(*) win_rate
   FROM trades WHERE pnl IS NOT NULL AND cascade_level != '' GROUP BY cascade_level;
   ```

3. **Per-regime expectancy**
   ```sql
   SELECT regime_at_entry, COUNT(*), AVG(CAST(pnl AS REAL)), AVG(CAST(fees_usd AS REAL)), AVG(CAST(funding_usd AS REAL))
   FROM trades WHERE pnl IS NOT NULL GROUP BY regime_at_entry;
   ```

4. **Maker vs taker P&L**
   ```sql
   SELECT maker_entry, COUNT(*), AVG(CAST(pnl AS REAL) - CAST(fees_usd AS REAL))
   FROM trades WHERE pnl IS NOT NULL GROUP BY maker_entry;
   ```

5. **Slippage cost**
   ```sql
   SELECT AVG(entry_slippage_bps), AVG(exit_slippage_bps), AVG(entry_slippage_bps + exit_slippage_bps)
   FROM trades WHERE pnl IS NOT NULL;
   ```

6. **Rejection distribution**
   ```sql
   SELECT stage, reason, COUNT(*) FROM decision_log
   WHERE outcome = 'reject' GROUP BY stage, reason ORDER BY 3 DESC LIMIT 20;
   ```

7. **Exit-reason mix**
   ```sql
   SELECT exit_reason_enum, COUNT(*), AVG(CAST(pnl AS REAL))
   FROM trades WHERE pnl IS NOT NULL GROUP BY exit_reason_enum;
   ```

8. **Per-symbol P&L with fee/funding drag**
   ```sql
   SELECT symbol, COUNT(*), SUM(CAST(pnl AS REAL)), SUM(CAST(fees_usd AS REAL)), SUM(CAST(funding_usd AS REAL))
   FROM trades WHERE pnl IS NOT NULL GROUP BY symbol;
   ```

9. **Confidence-bucket win rate**
   ```sql
   SELECT confidence_bucket, COUNT(*),
          SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1 ELSE 0 END)*1.0/COUNT(*) win_rate
   FROM trades WHERE pnl IS NOT NULL GROUP BY confidence_bucket;
   ```

10. **Funding-filter impact**
    ```sql
    SELECT outcome, COUNT(*) FROM decision_log WHERE stage = 'funding_filter' GROUP BY outcome;
    ```

11. **Consensus-adjustment impact on expectancy**
    ```sql
    SELECT CASE WHEN consensus_adj > 0 THEN 'boosted' WHEN consensus_adj < 0 THEN 'penalised' ELSE 'neutral' END b,
           COUNT(*), AVG(CAST(pnl AS REAL))
    FROM trades WHERE pnl IS NOT NULL GROUP BY b;
    ```

12. **Cycle completion latency**
    ```sql
    SELECT DATE(timestamp) d, AVG(duration_seconds), MAX(duration_seconds), COUNT(*)
    FROM cycle_history GROUP BY d ORDER BY d DESC LIMIT 14;
    ```

---

## 5. Dashboards / Reports Required

### 5.1 Daily Attribution Report
- Runs at 00:05 UTC.
- Writes `docs/reports/YYYY-MM-DD-attribution.md`.
- Contains: funnel conversion rates, per-cascade expectancy, per-regime expectancy, fee+funding drag summary, top 3 rejection reasons, maker-rate %, exit-reason mix.

### 5.2 Weekly Council Pre-Brief
- Runs Monday 00:15 UTC.
- Generates the evidence bundle that `council-of-five` requires (paste-ready SQL outputs).
- Purpose: whenever the council is invoked that week, the evidence pack is warm.

### 5.3 Real-Time Terminal Dashboard (optional, low-priority)
- Rich-table display of open positions + last 5 cycles' decision-funnel top-line.
- Read-only; separate process, shares SQLite via WAL concurrent-read.

---

## 6. Acceptance Criteria (copy into Phase-1 acceptance)

Phase 1 is DONE when ALL of the following are true for 7 consecutive calendar days:

- Every `cycle_history` row has ≥ N matching `decision_log` rows (N = count of symbols × stages reached).
- Every `trades` row with `exit_price IS NOT NULL` has populated: `cascade_level`, `confidence_bucket`, `regime_at_entry`, `fees_usd`, `funding_usd`, `exit_reason_enum`, `entry_slippage_bps`, `exit_slippage_bps`, `maker_entry`.
- Queries 1–12 return non-empty rows.
- Daily attribution report generated on 7 of 7 days.

---

## 7. Don't-Do List

- Don't store computed rates as derived columns. Compute at query time.
- Don't conflate `rejected` with `skipped` (skipped = stage intentionally bypassed; rejected = stage evaluated and said no).
- Don't let `decision_log` grow without a retention policy. Start with: `DELETE FROM decision_log WHERE timestamp_utc < DATE('now','-90 day')` nightly. Revisit when row count >5M.
- Don't log secrets in `numeric_context`. Ever.
- Don't compute P&L from `decision_log`. The canonical P&L source is `trades.pnl`.
