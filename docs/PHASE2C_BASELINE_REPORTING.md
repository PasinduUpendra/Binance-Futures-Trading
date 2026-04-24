# PHASE2C_BASELINE_REPORTING.md

> **Date:** 2026-04-22
> **Scope:** Phase 2C of `docs/MASTER_ROADMAP.md` — establish a clean "baseline epoch" reporting window for the mainnet reduced-live era.
> **Rule:** **Additive and reversible.** No trade is deleted. No historical row is mutated. No live trading logic changed. No strategy parameter tuned. No pair universe change. No circuit-breaker constant touched. No execution behavior modified.

---

## 1. Purpose

Cumulative reporting over the canonical DB (`user_data/claude_quant.db`) mixes:

- Pre-reduced-live testnet and mainnet rows (balance up to $5,000+),
- Pre-Phase-2B mainnet rows across 8 pairs with stale instrumentation,
- The current Phase 2B reduced-live era (2 pairs: `SOL`, `SUI`; $68.33 start balance).

These eras had different risk envelopes, different strategy surfaces, and different data quality. Aggregating their PnL, win rate, or expectancy into a single headline **cannot** be used to decide whether the current code is working.

Phase 2C introduces an **additive** baseline marker. The canonical trade history stays intact; reporting gains a `since=` view that filters to rows at or after the current baseline. The all-history view remains available (default). When new full-surface-area eras begin, a new baseline is set — the prior baseline is archived for single-step restore.

---

## 2. Files changed

| File | Change type | Description |
|---|---|---|
| `src/data/database.py` | Modified | Added `BaselineRow` Pydantic model + 5 methods: `set_baseline`, `get_baseline`, `clear_baseline`, `get_previous_baseline`, `restore_previous_baseline`. Uses existing `system_state` table — no schema migration. |
| `src/reporting/forensic_queries.py` | Modified | Added optional `since: datetime \| None` to every Q1–Q12 method. Backward-compatible (default `None` preserves Phase 1C behavior). Parameterized `_apply_since()` predicate injector. |
| `src/reporting/attribution_report.py` | Modified | `generate()` / `build_content()` accept `since=` and `baseline_meta=`. Header announces filter; filename gets `-since-baseline` suffix when active. Per-render context reset after each call. |
| `scripts/set_mainnet_baseline.py` | **NEW** | CLI for `--set`, `--show`, `--clear`, `--restore-previous`. Requires `--yes` to skip prompt. |
| `scripts/gen_attribution_report.py` | Modified | Added `--since-baseline` (reads current baseline from DB) and `--since <iso>` flags. Mutually exclusive. |
| `tests/test_reporting/test_baseline.py` | **NEW** | 29 tests: `BaselineRow` validation, DB CRUD + archive, `ForensicQueries` since filter, `AttributionReporter` since header/filename, backward compat, historical-integrity guard. |
| `docs/PHASE2C_BASELINE_REPORTING.md` | **NEW** | This file. |
| `docs/CURRENT_STATE.md` | Minor addition | §5 Persistence Model — baseline keys in `system_state` documented. |
| `docs/DRIFT_MAP.md` | Unchanged | No existing drift item was strictly resolved by Phase 2C; this is a new reporting capability, not a drift fix. |

**No changes to:** `src/orchestrator/main.py`, any `src/strategies/*`, any `src/risk/*`, any `src/execution/*`, any `config/*.yaml`, or any live code path. No `trades`, `cycle_history`, `decision_log`, `daily_reports`, or `fill_events` row is read, written, or migrated by Phase 2C code.

---

## 3. Baseline state design

The baseline is stored as 5 rows in the existing `system_state` (key-value) table. Keys:

| Key | Type | Example |
|---|---|---|
| `baseline.current_mode` | TEXT | `mainnet_reduced_live_v1` |
| `baseline.started_at_utc` | ISO-8601 TEXT | `2026-04-22T00:00:00+00:00` |
| `baseline.start_balance_usdt` | Decimal TEXT | `68.33` |
| `baseline.notes` | TEXT (may be empty) | `Phase 2B reduced-live cohort` |
| `baseline.set_at_utc` | ISO-8601 TEXT | `2026-04-22T14:12:30+00:00` |

Plus the archive slot (written whenever a new baseline overwrites or clears the current one):

| Key | Type | Contents |
|---|---|---|
| `baseline.previous_json` | JSON TEXT | `{current_mode, started_at_utc, start_balance_usdt, notes, set_at_utc, archived_at_utc}` |

Only one active baseline and one archive slot exist at a time (one-level undo). This is intentional: the intent is a **reporting cursor**, not a full history table. Deeper history is already present in `CHANGELOG.md` and in Phase-1C attribution reports.

### Why the `system_state` table and not a new table

- Zero schema migration risk.
- Mirrors the Sprint-1 consolidation pattern already used for `drawdown.*` and `daily.*` namespaces.
- Every existing `DatabaseManager` consumer ignores unknown `system_state` keys, so older code paths remain unaffected.

### Pydantic `BaselineRow` guarantees

- `frozen=True` (immutable).
- `current_mode` must be non-empty.
- `started_at_utc` and `set_at_utc` must be timezone-aware; naive datetimes raise `ValueError` at construction.
- All datetimes are normalized to UTC internally.
- `start_balance_usdt` is a `Decimal` (consistent with the monetary-value convention in `CLAUDE.md` §13).

---

## 4. CLI usage

### Show the current baseline

```bash
.venv/bin/python scripts/set_mainnet_baseline.py --show
```

Prints current baseline and previous archive (if any), or `Current baseline: NONE`.

### Set a new baseline

```bash
.venv/bin/python scripts/set_mainnet_baseline.py \
    --set \
    --mode mainnet_reduced_live_v1 \
    --balance 68.33 \
    --notes "Phase 2B reduced-live cohort starts here"
```

By default `--started-at` = `now UTC`. Override with an explicit timezone-aware timestamp:

```bash
.venv/bin/python scripts/set_mainnet_baseline.py \
    --set --mode foo --balance 68.33 \
    --started-at 2026-04-22T00:00:00+00:00
```

The command prompts for confirmation unless `--yes` is passed. If a baseline already exists, it is archived into `baseline.previous_json` before the new one is written.

### Clear the current baseline (archive for restore)

```bash
.venv/bin/python scripts/set_mainnet_baseline.py --clear
```

Exit 3 if no baseline is set.

### Restore the archived baseline

```bash
.venv/bin/python scripts/set_mainnet_baseline.py --restore-previous
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | validation error (missing arg, bad decimal, naive datetime, corrupt archive) |
| 2 | aborted by user at confirmation prompt |
| 3 | no baseline / archive present for the requested action |

---

## 5. Generating "since-baseline" reports

### All-history view (Phase 1C default; unchanged)

```bash
.venv/bin/python scripts/gen_attribution_report.py
# writes docs/reports/YYYY-MM-DD-attribution.md
```

### Since-baseline view (Phase 2C new)

```bash
.venv/bin/python scripts/gen_attribution_report.py --since-baseline
# writes docs/reports/YYYY-MM-DD-attribution-since-baseline.md
```

Reads the current baseline from `system_state` and injects it as a `since=` filter into every Q1–Q12 query. The report's title gets a `(SINCE BASELINE)` suffix; a highlighted callout at the top declares the active filter, the baseline mode, start balance, and notes.

### Arbitrary cutoff (ad hoc)

```bash
.venv/bin/python scripts/gen_attribution_report.py --since 2026-04-22T00:00:00+00:00
```

`--since-baseline` and `--since` are mutually exclusive. `--since` requires a timezone-aware ISO-8601 string.

### Programmatic usage

```python
from datetime import date
from src.data.database import DatabaseManager
from src.reporting.attribution_report import AttributionReporter

db = DatabaseManager()
baseline = db.get_baseline()
reporter = AttributionReporter(db)
path = reporter.generate(
    report_date=date.today(),
    since=baseline.started_at_utc if baseline else None,
    baseline_meta={
        "current_mode": baseline.current_mode,
        "start_balance_usdt": str(baseline.start_balance_usdt),
        "notes": baseline.notes,
    } if baseline else None,
)
```

---

## 6. Report behavior before vs after baseline

| Aspect | Before (Phase 1C only) | After Phase 2C (no baseline set) | After Phase 2C (`--since-baseline`) |
|---|---|---|---|
| Output filename | `YYYY-MM-DD-attribution.md` | `YYYY-MM-DD-attribution.md` (unchanged) | `YYYY-MM-DD-attribution-since-baseline.md` |
| Header | Generated-at only | Generated-at only (unchanged) | Adds baseline-filter callout with mode, start balance, notes |
| `trades`-derived sections (Q2, Q3, Q4, Q5, Q7, Q8, Q9, Q11) | All rows with `pnl IS NOT NULL` | Same | Rows also satisfy `timestamp >= baseline.started_at_utc` |
| `decision_log`-derived sections (Q1, Q6, Q10) | All rows | Same | Rows also satisfy `timestamp_utc >= baseline.started_at_utc` |
| `cycle_history`-derived sections (Q12) | All rows, last 14 days | Same | Rows also satisfy `timestamp >= baseline.started_at_utc` |
| `trades` / `cycle_history` / `decision_log` table contents | untouched | untouched | untouched |

If the baseline start is later than the latest trade, the report renders normally but with the "no data yet" placeholders — the header still shows which baseline was applied.

---

## 7. Rollback

Phase 2C is fully reversible without code changes.

### Full rollback

1. Delete the Phase 2C baseline keys:
   ```bash
   .venv/bin/python scripts/set_mainnet_baseline.py --clear --yes
   ```
2. Do not pass `--since-baseline` to `gen_attribution_report.py`. The default behavior is identical to Phase 1C.

### Partial rollback (revert a bad baseline decision)

```bash
.venv/bin/python scripts/set_mainnet_baseline.py --restore-previous --yes
```

Restores the last archived baseline.

### Code rollback (git revert)

```bash
git revert <phase-2c-commit-sha>
```

Removes the new code paths. Historical data is unaffected because Phase 2C never writes to `trades`, `cycle_history`, `decision_log`, `daily_reports`, or `fill_events`. The `baseline.*` rows in `system_state` can be deleted manually (or left — they become orphan KV entries ignored by all code).

---

## 8. Verification commands

### 8.1 DB methods exist

```bash
.venv/bin/python -c "
from src.data.database import DatabaseManager, BaselineRow
db = DatabaseManager('/tmp/verify.db')
assert hasattr(db, 'set_baseline')
assert hasattr(db, 'get_baseline')
assert hasattr(db, 'clear_baseline')
assert hasattr(db, 'get_previous_baseline')
assert hasattr(db, 'restore_previous_baseline')
print('OK — baseline API present')
"
```

### 8.2 Targeted tests

```bash
.venv/bin/python -m pytest tests/test_reporting/test_baseline.py -v
```

Expected: **29 passed**.

### 8.3 Full suite

```bash
.venv/bin/python -m pytest tests/ 2>&1 | tail -3
```

Expected: **862 passed, 3 warnings** (833 + 29 Phase 2C additions; Phase 1C's 74 reporting tests untouched).

### 8.4 CLI dry-run (sandbox DB)

```bash
.venv/bin/python scripts/set_mainnet_baseline.py --db /tmp/sandbox.db --set \
    --mode test --balance 100.00 --yes
.venv/bin/python scripts/set_mainnet_baseline.py --db /tmp/sandbox.db --show
.venv/bin/python scripts/set_mainnet_baseline.py --db /tmp/sandbox.db --clear --yes
rm /tmp/sandbox.db*
```

### 8.5 Since-query SQL shape (sanity)

```bash
.venv/bin/python -c "
from src.reporting.forensic_queries import ForensicQueries, Q2_PER_CASCADE_EXPECTANCY
sql, params = ForensicQueries._apply_since(Q2_PER_CASCADE_EXPECTANCY, '2026-04-22T00:00:00+00:00')
print(sql)
print('params =', params)
"
```

Expected output includes `WHERE timestamp >= ? AND pnl IS NOT NULL`.

---

## 9. Explicitly NOT done (hard constraints)

- ❌ No strategy file modified.
- ❌ No threshold tuned.
- ❌ No pair-universe change.
- ❌ No circuit-breaker constant changed.
- ❌ No execution / order path touched.
- ❌ No reduced-live flag changed.
- ❌ No historical trade / cycle / decision-log / daily-report / fill-event row deleted, mutated, or migrated.
- ❌ No schema migration (only additive uses of the existing `system_state` KV table).
- ❌ No new daemon / scheduled job / live hook — the baseline is read only on demand by reporting CLIs.

---

## 10. Council-of-Five-Lite review

**Paranoid Auditor:** `set_baseline` and `clear_baseline` write *only* to `system_state`; the 5 tests `test_historical_trades_untouched_by_baseline_ops` (×1) and `test_historical_trades_untouched_after_since_query` (×1) assert zero mutation to the `trades` table across every baseline lifecycle event. SQL injection is structurally impossible: all timestamps are passed as bound parameters, never interpolated into SQL strings; `_apply_since` treats the SQL as input and the timestamp as input, and concatenation is limited to a hard-coded column name drawn from a closed whitelist (`_TABLE_TS_COL`).

**Forensic Data Engineer:** `_apply_since` has three fall-throughs (WHERE → GROUP BY → ORDER BY → tail-append) and short-circuits to the original SQL when no recognized table is referenced. This matches the three patterns present in Q1–Q12 (`WHERE … GROUP BY`, `GROUP BY` without `WHERE`, and `WHERE …` without `GROUP BY`). Timestamp normalization is performed at a single choke point (`_to_iso`); the rest of the code assumes UTC.

**Deletionist:** Phase 2C adds 1 Pydantic model, 5 DB methods, 1 helper (`_apply_since`) + 12 thin kwarg-only call-site updates, 1 CLI, and 2 CLI flags. No new table. No new dependency. No new daemon. `clear_baseline` returns `False` rather than throwing when nothing is set — callers can distinguish "nothing to do" from "operation failed" without exceptions-as-control-flow. Defensible.

**QA Gremlin:** The per-render `_since` context attribute on `AttributionReporter` is cleared in a `try/finally`, so an exception raised inside a section helper cannot leak the filter into a subsequent call. `test_since_context_reset_after_render` covers the happy path. The CLI requires `--yes` to skip confirmation and distinguishes "aborted by user" (exit 2) from "nothing to do" (exit 3) from "validation error" (exit 1). Acceptable.

**Scale Predator:** Baseline keys are 5 rows at most; `system_state` has ~10 live keys already. `_apply_since` runs a single `re.sub` over short SQL strings (all Q1–Q12 bodies < 500 chars) — negligible cost. The `since=` filter short-circuits when None, preserving the Phase 1C hot-path performance for in-process attribution reporting.

---

**Last Updated:** 2026-04-22
**Test count (targeted):** 29 passed
**Test count (full suite):** 862 passed, 3 warnings (pre-existing)
**Code additions:** 0 strategy/exec changes; 0 schema migrations
