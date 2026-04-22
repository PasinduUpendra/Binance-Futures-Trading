# INFRA_DECISION.md — Runtime + Persistence Choice

> **Date:** 2026-04-22
> **Decision:** **Keep local SQLite + add process supervisor + consolidate JSON state.** Do NOT migrate to Postgres or AWS RDS at this balance scale.
> **Horizon:** Re-evaluate when balance ≥ $1,000 AND live-verified expectancy > 0.5%/day for ≥30 days.

---

## The Three Options

### Option A — Remote Runtime (VPS) + Local SQLite
Run the bot on a small VPS (Hetzner CAX11 €3.29/mo, Linode Nanode $5/mo, or similar). SQLite on the VPS's SSD. Claude Code sessions can SSH in. Process supervised by systemd.

**Pros**
- Cheapest. Matches current code (no migration).
- SQLite at current write rate (~1 write / 30 min) is far below its comfort zone.
- Physical network distance to Binance comparable to local laptop; no latency regression.
- Backups trivial: rsync nightly to another host; `.db` is a single file.
- Secrets: `/etc/claude-quant/env` chmod 600, owned by the service user.

**Cons**
- Single point of failure (VPS goes down → bot halts). Mitigation: systemd + external heartbeat monitor.
- You (human) are still the recovery operator.
- Backups are file-level, not point-in-time.

**Migration risk: NONE** (same stack as current code).
**Code impact: MINIMAL.** Drop in systemd unit, set env path. Maybe 30 lines of ops config.

---

### Option B — Managed Postgres (Supabase / Neon / Railway)
Move `trades`, `daily_reports`, `cycle_history`, `system_state`, `strategy_metrics`, `trailing_stops` into Postgres. Orchestrator runs locally or on VPS, connects via TCP.

**Pros**
- Schema introspection + better query tooling.
- Connection pooling, concurrent reads (e.g., a dashboard process).
- Row-level time-travel on some providers (Neon branching).
- Free tiers available (Supabase 500MB free; Neon generous free tier).

**Cons**
- **Network hop on every write.** At 30-min cycles × ~20 decision_log rows per cycle, that's ~40 writes/hour. Latency not a problem, but connection drop = orchestrator hang unless we add retry.
- New dependency (psycopg / asyncpg), new connection-string secret.
- SQLite → Postgres type mapping requires care for `Decimal`-as-TEXT fields (the current schema stores money as TEXT).
- Free-tier eviction: some providers sleep the DB after inactivity → first write after idle adds 1–5s. At 30-min cycles this probably catches every cycle.

**Migration risk: MEDIUM.** `DatabaseManager` is well-isolated; swap is real work but not scary. Candle cache stays SQLite (tables-per-pair pattern doesn't fit managed Postgres billing).

**Code impact:** ~500 lines touched. New connection-config module. Replace `sqlite3.connect` + `conn.execute(...)` with asyncpg pool. Rewrite migrations in Alembic. Schema rewrites for candle tables.

---

### Option C — AWS RDS (Postgres)
Managed Postgres on AWS. Same as Option B conceptually, but on AWS specifically.

**Pros**
- Enterprise-grade reliability.
- Point-in-time recovery with automated backups.
- Same-AZ deployment next to a future Lambda/ECS-hosted bot would cut latency to sub-millisecond.
- Integrations: CloudWatch metrics, SNS alerts, IAM auth.

**Cons**
- **Cost.** Smallest `db.t4g.micro` with 20GB storage is ~$15–25/mo depending on region. That's 20–35% of current balance **per month**. Unjustifiable at this scale.
- AWS secret management (Secrets Manager, IAM) adds real complexity. Reasonable at $10K+ balance; wasteful at $68.
- VPC/security-group plumbing is a trap for a solo operator.
- Egress costs if the bot runs outside AWS.

**Migration risk: HIGH.** Everything in Option B plus AWS-specific ops.
**Code impact:** Same as Option B + IAM/Secrets Manager integration.

---

## Scoring (weighted by what matters NOW at $68 balance)

| Criterion | Weight | A (SQLite+VPS) | B (Managed PG) | C (RDS) |
|---|---:|---:|---:|---:|
| Fits current balance scale (cost ≤ 5% of capital/mo) | 5 | ✅ | ⚠️ (free tier) | ❌ |
| Migration risk | 4 | ✅ | ⚠️ | ❌ |
| Disaster recovery | 3 | ⚠️ (manual) | ✅ | ✅ |
| Introspection / query power | 3 | ⚠️ | ✅ | ✅ |
| Ops complexity (solo operator) | 4 | ✅ | ⚠️ | ❌ |
| Future scalability | 2 | ⚠️ | ✅ | ✅ |
| **Weighted total** | — | **17** | **11** | **5** |

A wins decisively at current scale.

---

## Recommended Path (this phase)

### 1. Stay on SQLite, add process supervisor

- Write `scripts/systemd/claude-quant.service` (or `launchd` plist for macOS).
- Auto-restart on exit (Restart=always, RestartSec=30).
- Log to stdout; rely on journald/launchd log rotation.

### 2. Consolidate JSON state into `system_state` table

- Move `trailing_stops.json`, `daily_state.json`, `drawdown_state.json` read/write paths into `DatabaseManager.{get,set}_state` calls or the `trailing_stops` table.
- Delete the JSON writers. Keep the JSON files as historical artifacts (chmod a-w).
- Delete the old `user_data/agent_state/trade_journal.db` (and `.db-shm`, `.db-wal`) — already migrated to `claude_quant.db`.

### 3. Nightly backup cron

- `sqlite3 user_data/claude_quant.db ".backup /backup/cq-$(date +%F).db"` at 00:05 UTC.
- 14-day retention; `gzip -9` after 24h.

### 4. External heartbeat

- A tiny second process (cron every 2 min) that queries `SELECT MAX(timestamp) FROM cycle_history`. If newest row is older than `3 × CYCLE_INTERVAL_SECONDS` (= 90 min), fire an alert (Telegram / ntfy.sh). Stop-gap until proper watchdog is deployed.

### 5. Secret hygiene

- `chmod 600 .env`.
- Remove production keys from laptop; store in 1Password / macOS Keychain, source into the service-user's shell only.
- Delete `BINANCE_API_KEY_PROD` / `BINANCE_API_SECRET_PROD` from `.env` if the laptop isn't where you run live. Keep only testnet there.

---

## When to Re-Evaluate

Move to Option B (managed Postgres — probably Neon or Supabase, not RDS) when ALL of these are true:
- Balance ≥ $1,000.
- Phase 4 (live-verified) complete with attribution telemetry in place.
- `cycle_history` + `decision_log` tables combined exceed 100K rows.
- You want a second read-only process (dashboard, forensic analyst UI) without risking reader/writer contention.

RDS becomes reasonable only when the bot is running on AWS-hosted infrastructure AND balance supports a $20/mo infra tier. That's not a near-term concern.

---

## Concrete Migration Outline (for future reference, Option A → Option B)

If/when migrating:
1. Freeze writes for the migration window (1 cycle = 30 min).
2. `sqlite3 user_data/claude_quant.db .dump > cq.sql`; rewrite TEXT monetary columns to NUMERIC(20,8) during import.
3. Import into Postgres; verify row counts match.
4. Point `DatabaseManager` to Postgres via connection string.
5. Keep SQLite file read-only as fallback for 30 days.
6. Candle cache (`candle_store.py` table-per-pair) stays SQLite — not worth moving; can be regenerated from Binance.

Estimated engineering effort: ~16 hours (8 code, 4 tests, 4 ops).

---

## Decision Summary

| Question | Answer |
|---|---|
| Where does the bot run? | VPS (preferred) or laptop with launchd supervisor |
| Where does state live? | SQLite WAL at `user_data/claude_quant.db` |
| Where do candles live? | Same SQLite file (candle_store.py tables) |
| Where do secrets live? | OS keychain / `.env` chmod 600, NEVER in git |
| How is disaster recovered? | Nightly backup to separate host, external heartbeat alert |
| When do we revisit? | Balance ≥ $1K, Phase 4 complete, ≥100K cycle_history rows |
