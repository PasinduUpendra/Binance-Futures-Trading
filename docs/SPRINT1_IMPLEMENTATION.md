# SPRINT1_IMPLEMENTATION.md — Persistence Unification, Supabase Mirror, launchd Supervision

> **Date:** 2026-04-22
> **Baseline tests:** 638 passing (pre-sprint). **Post-sprint:** 662 passing (+24).
> **Code is truth.** Every claim below cites the file:line it is grounded in.
> **In-scope only.** This sprint does NOT touch strategy thresholds, circuit-breaker constants, pair universe, or Docker.

---

## A. Write-path audit (before Sprint 1)

| # | DB / File | Writer in code | Status |
|---|---|---|---|
| 1 | `user_data/claude_quant.db / trades` | *(none)* | ❌ canonical table had 0 rows — split-brain |
| 2 | `user_data/claude_quant.db / cycle_history` | `main.py:2989` `self.db.store_cycle()` | ✅ 759 rows |
| 3 | `user_data/claude_quant.db / daily_reports` | `main.py:2896` `self.db.store_daily_report()` | ✅ 19 rows |
| 4 | `user_data/claude_quant.db / trailing_stops` | `main.py:3148` `self.db.upsert_trailing_stop()` | ✅ 1 row |
| 5 | `user_data/claude_quant.db / system_state` | one-shot helpers only | ⚠ no live writers |
| 6 | `user_data/agent_state/trade_journal.db / trades` | `trade_journal.py:300` `record_trade()` via `main.py:1425` / `:1512` | ❌ 17 rows — the live trades |
| 7 | `user_data/audit_trail.db / audit_trail` | `decision_auditor.py:378` `_persist()` | ⚠ 95 rows in an isolated file |
| 8 | `user_data/data/trades.db` | *(none, no tables)* | 💀 dead file |
| 9 | `user_data/agent_state/daily_state.json` | `main.py:3057` `_persist_daily_state()` | 🔁 dual-write |
| 10 | `user_data/agent_state/drawdown_state.json` | `drawdown_monitor.py:148` `_persist_state()` | 🔁 not mirrored in DB |
| 11 | `user_data/agent_state/trailing_stops.json` | `main.py:3167` legacy-compat write | 🔁 dual-write |
| 12 | `user_data/agent_state/last_cycle.json` | `main.py:3005` | ⚠ read by `watchdog_tools.py` — left intact |
| 13 | `user_data/agent_state/watchdog_state.json` | `scripts/watchdog.py:151` (legacy) | out-of-scope (not orchestrator) |

**Root cause of split-brain:** two SQLite clients constructed independently in `Orchestrator.__init__` — `TradeJournal` at line 223 (pointed at `trade_journal.db`) and `DatabaseManager` at line 238 (pointed at `claude_quant.db`). Each owned its own `trades` table. Only the legacy one received live writes.

---

## B. Exact files changed

| File | Change |
|---|---|
| [src/data/database.py](../src/data/database.py) | +`audit_trail` table schema/indexes; +`insert_audit` / `get_audit_by_trade` / `get_recent_audits`; +`attach_mirror` / `_mirror_enqueue`; mirror hooks in `store_cycle`, `store_daily_report`, `upsert_trailing_stop`, `delete_trailing_stop`, `insert_audit`; +`migrate_daily_state`, `migrate_trailing_stops_json`, `migrate_from_audit_trail` |
| [src/data/supabase_mirror.py](../src/data/supabase_mirror.py) | **new** — thread-backed queue, PostgREST upsert (`Prefer: resolution=merge-duplicates`), retries with backoff, no-op when env vars missing, singleton helper |
| [src/memory/trade_journal.py](../src/memory/trade_journal.py) | +`attach_mirror`; mirror hooks in `record_trade` and `update_trade_exit` (including standalone exits) |
| [src/anti_hallucination/decision_auditor.py](../src/anti_hallucination/decision_auditor.py) | Default DB path → `user_data/claude_quant.db`; +`attach_mirror`; mirror hook in `_persist` |
| [src/risk/drawdown_monitor.py](../src/risk/drawdown_monitor.py) | +`attach_db` / `db=` kwarg; `_persist_state` DB-first with JSON fallback; `_load_state` DB-first with legacy-JSON migration path |
| [src/orchestrator/main.py](../src/orchestrator/main.py) | `TradeJournal(db_path=self.db.db_path)` (points at canonical DB); `DecisionAuditor(db_path=...)`; `SupabaseMirror` construction + `attach_mirror` on db/journal/auditor; `attach_db` on drawdown_monitor; `_load_daily_state` / `_persist_daily_state` rewritten to use `system_state`; stopped legacy JSON side-write in `_persist_trailing_stop_state`; `self.mirror.close()` in shutdown |
| [scripts/migrate_to_canonical_db.py](../scripts/migrate_to_canonical_db.py) | **new** — idempotent migration CLI with `--dry-run` / `--no-archive` |
| [scripts/heartbeat_monitor.py](../scripts/heartbeat_monitor.py) | **new** — stdlib-only stale-cycle detector |
| [scripts/launchd/run_bot.sh](../scripts/launchd/run_bot.sh) | **new** — env-sourcing wrapper |
| [scripts/launchd/com.claudequant.bot.plist](../scripts/launchd/com.claudequant.bot.plist) | **new** — auto-restart on crash, throttled to 30 s |
| [scripts/launchd/com.claudequant.heartbeat.plist](../scripts/launchd/com.claudequant.heartbeat.plist) | **new** — 5-minute interval watcher |
| [tests/test_data/test_supabase_mirror.py](../tests/test_data/test_supabase_mirror.py) | **new** — 9 tests (enabled/disabled, happy path, retries, failures, queue-full, singleton, conflict map) |
| [tests/test_data/test_canonical_migration.py](../tests/test_data/test_canonical_migration.py) | **new** — 8 tests (trade-journal, audit, daily, trailing, missing files, idempotency, insert_audit) |
| [tests/test_data/test_heartbeat_monitor.py](../tests/test_data/test_heartbeat_monitor.py) | **new** — 5 subprocess-level tests for exit codes |
| [tests/test_risk/test_drawdown_monitor.py](../tests/test_risk/test_drawdown_monitor.py) | +2 tests (DB-backed persistence + DB-preferred-over-JSON load) |

---

## C. Migration — operational procedure

```bash
# 1. Baseline — verify the test suite is green
python3 -m pytest tests/ -q           # expect: 662 passed

# 2. Dry-run first so you can see what's about to happen
python3 scripts/migrate_to_canonical_db.py --dry-run

# 3. Actually migrate. Idempotent — safe to re-run.
python3 scripts/migrate_to_canonical_db.py

# 4. Verify the canonical DB is now the single source of truth
sqlite3 user_data/claude_quant.db \
  "SELECT 'trades', COUNT(*) FROM trades
     UNION ALL SELECT 'audit_trail',   COUNT(*) FROM audit_trail
     UNION ALL SELECT 'trailing_stops',COUNT(*) FROM trailing_stops
     UNION ALL SELECT 'system_state',  COUNT(*) FROM system_state
     UNION ALL SELECT 'cycle_history', COUNT(*) FROM cycle_history
     UNION ALL SELECT 'daily_reports', COUNT(*) FROM daily_reports;"

# 5. Confirm the legacy files have been archived
ls -la user_data/agent_state/archive/
```

**Expected after migration (verified by `--dry-run` on the real repo state):**

| Table | Rows |
|---|---:|
| `trades` | **17** (all legacy trades imported) |
| `audit_trail` | **95** (all legacy audit rows imported) |
| `trailing_stops` | 1 |
| `system_state` | 5 (drawdown keys) + up to 4 (daily keys) |
| `cycle_history` | 759 |
| `daily_reports` | 19 |

---

## D. Rollback plan

All legacy files are **archived, not deleted** (`user_data/agent_state/archive/*.YYYYMMDD-HHMMSS`). To roll back:

```bash
# 1. Stop the bot
launchctl bootout gui/$UID/com.claudequant.bot 2>/dev/null || true

# 2. Restore legacy files from archive
cd user_data/agent_state/archive
for f in trade_journal.db*.2026*; do
    cp "$f" "../$(echo $f | sed 's/\.2026[0-9-]*$//')"
done
cp audit_trail.db.* ../../audit_trail.db  # pick the latest timestamp

# 3. Revert the code (git)
git revert <sprint-1-commit>   # when the sprint is committed

# 4. Confirm legacy write paths are restored
sqlite3 user_data/agent_state/trade_journal.db "SELECT COUNT(*) FROM trades;"
```

The canonical DB file itself is **never overwritten** by the migration — only new rows appended with `INSERT OR IGNORE` — so a rollback simply means re-pointing the bot at the legacy files. No data is lost in either direction.

---

## E. Supabase mirror — setup

Designed as **remote mirror + analytics + backup only**. Local SQLite stays authoritative; the mirror is non-blocking, best-effort, and fails silently.

### 1. Provision a Supabase project (free tier is fine)

Create these tables with the schema matching the local SQLite tables (copy-paste-friendly SQL):

```sql
CREATE TABLE IF NOT EXISTS trades (
    trade_id       TEXT PRIMARY KEY,
    timestamp      TEXT NOT NULL,
    symbol         TEXT,
    direction      TEXT,
    entry_price    TEXT,
    exit_price     TEXT,
    size           TEXT,
    leverage       INTEGER,
    pnl            TEXT,
    pnl_pct        TEXT,
    strategy       TEXT,
    regime         TEXT,
    confidence     TEXT,
    stop_loss      TEXT,
    take_profit    TEXT,
    duration       REAL,
    fees           TEXT,
    slippage       TEXT,
    reasoning      TEXT,
    lessons        TEXT,
    mode           TEXT,
    signal_tag     TEXT,
    exit_reason    TEXT
);

CREATE TABLE IF NOT EXISTS cycle_history (
    cycle_number            INTEGER PRIMARY KEY,
    timestamp               TEXT,
    circuit_breaker_level   TEXT,
    balance                 TEXT,
    regime                  TEXT,
    signal_generated        BOOLEAN,
    trade_placed            BOOLEAN,
    trade_details           TEXT,
    positions_closed        TEXT,
    errors                  TEXT,
    duration_seconds        REAL
);

CREATE TABLE IF NOT EXISTS daily_reports (
    report_date     TEXT PRIMARY KEY,
    start_balance   TEXT, end_balance TEXT,
    realized_pnl    TEXT, unrealized_pnl TEXT,
    fees            TEXT, net_pnl TEXT, pnl_pct TEXT,
    trades_count    INTEGER, wins INTEGER, losses INTEGER,
    strategies_used TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS trailing_stops (
    symbol          TEXT PRIMARY KEY,
    direction       TEXT, entry_price REAL, best_price REAL,
    atr_4h          REAL, activated BOOLEAN,
    strategy_name   TEXT, take_profit REAL, tp_pending BOOLEAN,
    updated_at      TEXT, deleted BOOLEAN
);

CREATE TABLE IF NOT EXISTS audit_trail (
    audit_id        TEXT PRIMARY KEY,
    trade_id        TEXT, timestamp TEXT,
    symbol          TEXT, direction TEXT,
    strategy_name   TEXT, regime TEXT, decision TEXT,
    report_json     TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS strategy_metrics (
    strategy      TEXT, regime TEXT,
    total_trades  INTEGER, wins INTEGER, losses INTEGER,
    win_rate      TEXT, avg_pnl TEXT, total_pnl TEXT,
    max_win       TEXT, max_loss TEXT, profit_factor TEXT,
    sharpe        REAL, last_updated TEXT,
    PRIMARY KEY (strategy, regime)
);
```

### 2. Disable RLS on these tables (the service-role key writes on behalf of the bot)

In the Supabase dashboard: **Database → Tables → <each table> → RLS → disable** for the mirror tables. Or leave RLS enabled and use the **service-role** key; the mirror already sends the correct headers.

### 3. Export the credentials on the laptop

```bash
# Append to ~/.zshenv OR to the repo .env
export SUPABASE_URL="https://xxxxxxxxxxxxxxxxxxxxx.supabase.co"
export SUPABASE_SERVICE_KEY="<service-role-key>"
```

### 4. Verification

```bash
# Start the bot once and look for the startup log line:
python3 scripts/run_bot.py 2>&1 | grep -i supabase
# Expect: "SupabaseMirror started (url=https://...)"
# If you forget the env, you see:
#   "SupabaseMirror disabled (SUPABASE_URL / SUPABASE_SERVICE_KEY not configured)"
```

A single smoke trade or an immediate call to `db.store_cycle(...)` from a Python shell will produce a row both in local SQLite **and** in the Supabase table within a few seconds.

### 5. Non-blocking guarantee

- Local write commits **before** anything is sent to Supabase.
- The mirror runs on its own daemon thread with a bounded queue (`10_000`).
- HTTP failures are retried up to 3 times with exponential backoff and then logged as a WARNING. They **never** propagate to the caller.
- If the queue is full, the **oldest** row is dropped to make room for the newest (see `SupabaseMirror.enqueue`).
- Covered by tests in [tests/test_data/test_supabase_mirror.py](../tests/test_data/test_supabase_mirror.py).

---

## F. launchd — install / uninstall / reload

All paths below are absolute. If you move the repo, edit the plists first.

### Install (first time)

```bash
# 1. Make scripts executable (idempotent)
chmod +x scripts/launchd/run_bot.sh scripts/heartbeat_monitor.py

# 2. Copy the plists into the user LaunchAgents dir
mkdir -p ~/Library/LaunchAgents
cp scripts/launchd/com.claudequant.bot.plist       ~/Library/LaunchAgents/
cp scripts/launchd/com.claudequant.heartbeat.plist ~/Library/LaunchAgents/

# 3. Bootstrap both services
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.claudequant.bot.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.claudequant.heartbeat.plist

# 4. Confirm they are loaded
launchctl print gui/$UID/com.claudequant.bot       | grep -E "state|path"
launchctl print gui/$UID/com.claudequant.heartbeat | grep -E "state|path"
```

### Tail the logs

```bash
tail -F user_data/logs/launchd.stdout.log user_data/logs/launchd.stderr.log
tail -F user_data/logs/heartbeat.log
```

### Reload after editing code

```bash
launchctl kickstart -k gui/$UID/com.claudequant.bot
```

### Uninstall

```bash
launchctl bootout gui/$UID/com.claudequant.bot
launchctl bootout gui/$UID/com.claudequant.heartbeat
rm ~/Library/LaunchAgents/com.claudequant.bot.plist
rm ~/Library/LaunchAgents/com.claudequant.heartbeat.plist
```

### Auto-restart behaviour

`KeepAlive.Crashed=true` means launchd restarts the bot **only** when it exits with a non-zero status or is killed by a signal. A clean shutdown (`await orchestrator.stop()` → exit 0) is respected. `ThrottleInterval=30` prevents thrashing on a persistent crash loop.

---

## G. Heartbeat monitor — design + verification

- Reads `SELECT MAX(timestamp) FROM cycle_history` from the canonical DB.
- Exit codes: `0` fresh · `2` stale · `3` DB missing / empty.
- Writes one log line to `user_data/logs/heartbeat.log` on non-zero exit.
- Polled every 5 minutes by the heartbeat plist.
- Pure stdlib — runs even if the venv is broken.

Manual smoke test (already verified on this repo):

```bash
python3 scripts/heartbeat_monitor.py
# 2026-04-22T11:30:49+00:00 HEARTBEAT_STALE last_cycle=2026-04-11T16:10:12... age_min=15560.6 threshold_min=90
# exit=2    (expected — the bot has not run since 2026-04-11)
```

---

## H. Secret hygiene

**Status at 2026-04-22:**

| Check | State |
|---|---|
| `.env` in `.gitignore` | ✅ yes (`.env`, `.env.local`, `.env.production`) |
| `.env` filesystem mode | ⚠ currently `-rw-r--r--` (644) → **should be 600** |
| Production Binance keys in `.env` | ⚠ present |
| Testnet Binance keys in `.env` | ⚠ present alongside prod |
| Supabase keys in `.env` | — not yet configured |

### Recommended local secret storage on macOS

Two options, pick one:

**Option 1 — Tighten `.env` permissions (minimum viable).**
```bash
chmod 600 /Users/pasinduupendra/Documents/Development/Claude\ Quant/.env
ls -la .env      # expect: -rw-------
```

**Option 2 — macOS Keychain (preferred for production keys).**

Store each secret in the login keychain:
```bash
security add-generic-password -a "$USER" -s CLAUDE_QUANT_BINANCE_KEY_PROD \
    -w "<api-key>" -U
security add-generic-password -a "$USER" -s CLAUDE_QUANT_BINANCE_SECRET_PROD \
    -w "<api-secret>" -U
security add-generic-password -a "$USER" -s CLAUDE_QUANT_SUPABASE_SERVICE_KEY \
    -w "<service-role-key>" -U
```

Then edit `scripts/launchd/run_bot.sh` to `security find-generic-password -s ... -w`:

```bash
export BINANCE_API_KEY_PROD="$(security find-generic-password -a "$USER" -s CLAUDE_QUANT_BINANCE_KEY_PROD -w)"
export BINANCE_API_SECRET_PROD="$(security find-generic-password -a "$USER" -s CLAUDE_QUANT_BINANCE_SECRET_PROD -w)"
export SUPABASE_SERVICE_KEY="$(security find-generic-password -a "$USER" -s CLAUDE_QUANT_SUPABASE_SERVICE_KEY -w)"
```

macOS will prompt the first time launchd tries to read the Keychain; after you click **Always Allow** it is unattended.

### Rotate the Supabase service-role key after initial setup

Any service-role key that passed through Slack / documents / screen shares must be treated as compromised. On day one:

1. Dashboard → **Project Settings → API → service_role → Reset**.
2. Update the Keychain entry or `.env`.
3. Reload launchd: `launchctl kickstart -k gui/$UID/com.claudequant.bot`.

### Never-dos (constitutional)

- Never commit `.env` (already gitignored).
- Never paste a production Binance key or Supabase service-role key into documentation, CHANGELOG, Slack, or issue trackers. Reference it by Keychain service name instead.
- Never share one `.env` between testnet and mainnet machines.
- Never put the service-role key in client-side or anywhere RLS can be bypassed without intent.

---

## I. Verification commands (copy-paste)

```bash
# 1. Full test suite
cd "/Users/pasinduupendra/Documents/Development/Claude Quant"
python3 -m pytest tests/ -q
# EXPECTED: 662 passed

# 2. New tests only
python3 -m pytest tests/test_data/test_supabase_mirror.py \
                  tests/test_data/test_canonical_migration.py \
                  tests/test_data/test_heartbeat_monitor.py \
                  tests/test_risk/test_drawdown_monitor.py -v
# EXPECTED: 48 passed

# 3. Migration dry-run
python3 scripts/migrate_to_canonical_db.py --dry-run
# EXPECTED:
#   [dry-run] would import up to 17 trades
#   [dry-run] would import up to 95 audit rows
#   (plus JSON state file detection and archive preview)

# 4. Plist validation
plutil scripts/launchd/com.claudequant.bot.plist
plutil scripts/launchd/com.claudequant.heartbeat.plist
# EXPECTED: OK for both

# 5. Wrapper script syntax
bash -n scripts/launchd/run_bot.sh
# EXPECTED: exit 0, no output

# 6. Heartbeat smoke test
python3 scripts/heartbeat_monitor.py; echo "exit=$?"
# EXPECTED (cycles are stale): exit=2, one line in user_data/logs/heartbeat.log

# 7. Post-migration row counts (after actually running step 3 without --dry-run)
sqlite3 user_data/claude_quant.db \
  "SELECT 'trades', COUNT(*) FROM trades
     UNION ALL SELECT 'audit_trail',   COUNT(*) FROM audit_trail;"
# EXPECTED: trades|17  audit_trail|95
```

---

## J. No-go rules honoured

| Rule | Evidence this sprint did not violate it |
|---|---|
| No strategy threshold changes | No edits to `adaptive_strategy.py`, `supertrend_trend.py`, `regime_detector.py` confidence / ADX / RSI gates |
| No new strategies | No new file under `src/strategies/` |
| No circuit-breaker constant changes | No edits to `circuit_breaker.py` |
| No live execution moved to Supabase | Supabase is **write-mirror only** — reads still come from local SQLite |
| No Docker | No Dockerfile / docker-compose edits; Docker removal is a laptop-level operator action |
| No pair universe change | No edits to `TRADING_PAIRS` |

---

## K. Next-sprint readiness checklist

- [x] One canonical DB (`claude_quant.db`) holds all live execution tables.
- [x] Legacy files archived, recoverable.
- [x] Supabase mirror runs non-blocking; disabled cleanly when unset.
- [x] launchd supervises the bot; heartbeat detects stalls.
- [x] Tests: 662 passing (baseline 638 + 24 new).
- [ ] *(out of scope)* `decision_log` table for Phase-1 forensics — see [LIVE_FORENSICS_SPEC.md](LIVE_FORENSICS_SPEC.md).
- [ ] *(out of scope)* Per-trade attribution columns (`cascade_level`, `fees_usd`, …) — Phase 1.
