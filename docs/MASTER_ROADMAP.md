# MASTER_ROADMAP.md — Phased Plan

> **Date:** 2026-04-22
> **Governing principle (Council of Five consensus):** You cannot optimize what you cannot measure. Build the measurement layer, freeze the surface area, close the doc/code drift, and only then tune.
> **Prefer smaller, meaner bot over broader, cleverer one.**

---

## Phase 0 — Freeze & Canonicalize (this session)

**Goal:** Stop the bot from mutating while drift exists. Produce a canonical memory pack.

**Tasks**
1. Write `docs/CURRENT_STATE.md`, `docs/DRIFT_MAP.md`, `docs/SESSION_BOOTSTRAP.md`, this roadmap, `docs/AGENT_PLAYBOOK.md`, `docs/INFRA_DECISION.md`, `docs/LIVE_FORENSICS_SPEC.md`, `.claude/agents/council_of_five.md`. (Done in this session.)
2. Delete/deprecate `docs/SYSTEM_REVIEW.md`, `docs/SINGLE_SOURCE_OF_TRUTH.md` (or explicitly mark them "ARCHIVED — see CURRENT_STATE.md").
3. Rewrite CLAUDE.md "Performance Reality" header to be honest: "Backtested ceiling: 2.68% (v6.16). Live-verified: N/A — requires ≥30 closed trades on current code." No other change to CLAUDE.md this phase.

**Acceptance criteria**
- `pytest tests/ --collect-only` count updated in CURRENT_STATE.md (replaces stale 598).
- No document outside `docs/CURRENT_STATE.md` references a specific daily-return figure as "validated" without a live-trade-count qualifier.
- `.claude/agents/watchdog.md` loses the `0.628%` references.

---

## Phase 1 — Live Forensics Instrumentation (blocks all strategy work)

**Goal:** Capture the decision funnel and per-trade attribution so every future decision can be evidence-based. Spec detail: [LIVE_FORENSICS_SPEC.md](LIVE_FORENSICS_SPEC.md).

**Tasks**
1. New `decision_log` table: row per (cycle_id, symbol, stage, outcome, reason, numeric_context). Stages: `data_fetch`, `regime_detect`, `signal_generate`, `cascade_level`, `confidence_gate`, `consensus_adjust`, `position_overlap`, `funding_filter`, `leverage`, `volatility_adjust`, `sizing`, `min_notional`, `liquidation_buffer`, `price_validate`, `signal_validate`, `audit`, `post_only`, `market_fill`, `sl_place`, `tp_place`, `trail_place`. Write one row per stage, always, not just on failure.
2. Extend `trades` table with attribution columns: `entry_slippage_bps`, `exit_slippage_bps`, `maker_entry` (bool), `maker_exit` (bool), `fees_usd`, `funding_usd`, `hold_bars`, `cascade_level` (flip/continuation/fast/aligned), `confidence_bucket`, `regime_at_entry`, `atr_at_entry`, `exit_reason_enum`.
3. Add `cycle_summary` view/report: decision funnel conversion rates.
4. Nightly job: `daily_attribution_report` that decomposes each closed trade's P&L into fee + funding + slippage + raw-edge.

**Acceptance criteria**
- After 7 days of live operation, `SELECT cascade_level, COUNT(*), SUM(pnl), AVG(pnl) FROM trades WHERE closed GROUP BY cascade_level` returns a non-empty table.
- Decision funnel report shows ≥1 row per (cycle, symbol) pair for every cycle since instrumentation began.
- No trade is recorded without ALL attribution columns populated.

---

## Phase 2 — Strategy & Feature Simplification

**Goal:** Remove live-path surface area that lacks evidence. Keep only what the cascade attribution proves valuable.

**Hard rule:** No removal until Phase 1 attribution exists. No tuning until ≥30 closed trades on current code under attribution.

**Candidates for removal (require attribution evidence before acting):**

| Candidate | Why it's suspect | Kill criterion |
|---|---|---|
| 15m fast-entry cascade level | 45 min detection window on 30-min polling is mostly redundant with 1H continuation | <5 closed trades OR net expectancy ≤ 0 over any 50-cycle window |
| Aligned-trend entry | Confidence ceiling 55 = at MIN_CONFIDENCE floor of 45, narrow band, late-momentum | <5 closed trades OR expectancy ≤ 0 |
| Cross-asset consensus ±10 | Adjustment smaller than 1 confidence-tier boundary; may just add noise | A/B: 30 trades with consensus ON vs OFF; keep if Δ Sharpe > 0.3 |
| BreakoutTrader in VOLATILE | Historically negative EV | <55% win rate OR <0 net after 20 trades |
| AdaptiveTrend in RANGING+ADX<18 | Sprint-2 add, limited live evidence | <55% win rate OR <0 net after 20 trades |
| Wrong-side force-close | Reactive whipsaw risk; threshold already raised 2 → 8 cycles | If ≥3 force-closes produce worse exit than trailing stop would have |
| Dynamic position limit (+1 in GREEN) | At current balance, 4th slot often can't meet min notional | Force-disable until balance ≥ $200 |

**Non-candidates (keep regardless of data):**
- SupertrendTrend 4H flip
- SupertrendTrend 1H continuation (it's what makes the bot trade more than 2×/month)
- All circuit-breaker hardcoded thresholds
- Partial TP at 1:1 R/R + SL-to-breakeven
- Post-only maker-first with taker fallback
- Native Binance trailing-stop safety net

**Acceptance criteria**
- Each removal/retention decision is backed by a SQL query on `trades` + `decision_log` pasted into the CHANGELOG entry.
- Strategy file stays on disk with `# DISABLED — see CHANGELOG YYYY-MM-DD` header (keep code for resurrection, remove the route).

---

## Phase 3 — Infra Hardening (parallelizable with Phases 1-2)

**Goal:** The bot should survive a laptop crash, a network outage, or a Claude-session death.

Detailed decision: [INFRA_DECISION.md](INFRA_DECISION.md). Summary:
- Stay on SQLite WAL for now.
- Consolidate the last two JSON state files (`daily_state.json`, `drawdown_state.json`) into `system_state` table; delete the JSON writers.
- Add a **process supervisor**: launchd on macOS / systemd on Linux. Auto-restart on exit.
- Add a **read-only monitor process** (separate from orchestrator) that polls `cycle_history` table every 60s and sends an alert if no new row for > 3 × `CYCLE_INTERVAL_SECONDS`.
- Migrate `.env` production keys out of plaintext: either OS keychain (macOS) or `sops`/age.

**Acceptance criteria**
- Kill -9 the bot process — it's back within 30s with state intact.
- Alert fires within 3 minutes of any cycle stall.
- No plaintext prod key anywhere in the repo or `.env` by end of phase.

---

## Phase 4 — Live-Verified Ceiling Calibration

**Goal:** Replace all "backtested X%" claims with "live-verified X%".

**Tasks**
1. Run bot for 30 calendar days uninterrupted on current code with Phase-1 attribution active.
2. Aggregate closed trades by {cascade-level, regime, symbol, confidence-bucket}.
3. Compute honest Sharpe (daily log-return), max drawdown, hit rate, expectancy, net-of-everything.
4. Update ONE document (`docs/CURRENT_STATE.md` "Live-Verified Performance" section).
5. Decide: does the live curve justify capital increase? Is the next milestone still $1000?

**Acceptance criteria**
- ≥30 closed trades captured with full attribution.
- No open trade older than `MAX_HOLD_BARS`.
- Drift-map review: do any Phase-1/2/3 changes require doc updates? If yes, update before declaring Phase 4 done.

---

## Phase 5 — Scale Gate (not earlier than Phase 4 complete)

**Goal:** Only at this gate does adding complexity, pairs, or capital make sense.

Pre-conditions (ALL must be true):
- Live-verified daily expectancy positive over ≥30 trades.
- Max live drawdown ≤ 15%.
- Zero drift between code and `CURRENT_STATE.md`.
- Zero unresolved attribution gaps.

Possible scale moves (in priority order, one at a time, attribution re-verified after each):
1. Re-add BTC to `TRADING_PAIRS` when balance ≥ $200 (min notional $100, want ≥ 2 slots worth).
2. Raise `MIN_CONFIDENCE` from 45 → 50 if aligned-trend is already killed and lower-tier trades are underperforming.
3. Add a new pair only if cascade attribution shows a pair would clear 55% win rate.
4. Capital injection +$100/+$500 — do NOT change any strategy parameter on the same day.

---

## Explicit No-Go Items (independent of any phase)

- **No new strategy files.** Zero. The repo already has 7 strategy classes and routes only 3. Nothing new merges.
- **No tuning of MIN_CONFIDENCE, ADX_MIN, RSI pullback thresholds, confidence-tier sizing, or ANY magic number** without ≥30 closed-trade attribution behind it.
- **No re-enabling MeanReversion, TrendFollower, or Scalper.**
- **No migration to Postgres/RDS at current scale.**
- **No adding `BINANCE_TESTNET=true` detour mid-cycle** — testnet and prod keys are never mixed in one `.env`.
- **No changing circuit-breaker hardcoded constants.** Including "just for testing".
- **No raising `DYNAMIC_POS_ABSOLUTE_MAX` above 5.**
- **No 15-minute or 5-minute cycle interval.** 30-min is already probably too aggressive for a 4H strategy; faster is worse.
- **No deploying a second bot instance against the same account.** Idempotent client IDs don't protect you from rapid-fire conflicting reconciliations.

---

## Dependency Order

```
Phase 0 (this session)  ─────►  Phase 1 (forensics)  ─────►  Phase 2 (simplify)
                                       │                           │
                                       ▼                           ▼
                                  Phase 3 (infra, in parallel)     │
                                       │                           │
                                       └──────────────┬────────────┘
                                                      ▼
                                              Phase 4 (live-verified metrics)
                                                      │
                                                      ▼
                                              Phase 5 (scale gate)
```

Phases 0, 1, 3 can start immediately. Phase 2 requires Phase 1 data. Phase 4 requires all prior. Phase 5 is gated on Phase 4 acceptance.

---

## Definition of Done — Master

This roadmap is complete when:
- The bot runs unattended for 30 consecutive days.
- Every open trade is attributable to a specific cascade level + regime with full fee/funding/slippage accounting.
- `CURRENT_STATE.md` has a "Live-Verified" section with numbers that are younger than 24 hours.
- No doc mentions 0.628%, 1.149%, 1.397%, 2.68% as "validated" — only as "backtested (date, config)".
- `.claude/agents/*.md` is either accurate or deleted.
