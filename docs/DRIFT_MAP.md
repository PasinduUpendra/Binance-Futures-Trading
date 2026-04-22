# DRIFT_MAP.md — Docs vs Code vs Agent Instructions

> **Date:** 2026-04-22
> **Rule:** CODE is truth. Docs and agent definitions must catch up, not the other way around.
> **Scope:** Every drift below is grounded in a file:line citation; no opinions, only deltas.

---

## 1. Big-Picture Narrative Drift (the "what is this bot even for" problem)

The "validated daily return" headline is different in every document. None of the numbers is live-verified on current code.

| Document | Headline claim | Origin |
|---|---|---|
| [CLAUDE.md (header table)](../CLAUDE.md) | **2.68% avg daily** | v6.16 AGG4 sweep, 197-trade backtest |
| [docs/SINGLE_SOURCE_OF_TRUTH.md §1](SINGLE_SOURCE_OF_TRUTH.md) | **1.149% daily validated from v5 sweep** | v5 backtest 75 trades |
| [.claude/agents/watchdog.md](../.claude/agents/watchdog.md) | **0.628% validated ceiling** | pre-v6.1 number, ~2026-03-15 |
| CHANGELOG v6.1 | 1.397% | 9-pair v6.1 backtest |
| Sprint-1 backtest row | 1.531% / 1.560% | Sprint 1 backtest |
| Sprint-2 backtest row | 1.728% | Sprint 2 backtest |

**Required action:** Every public document must stop claiming a validated daily figure. Replace with "backtested ceiling (not live-verified): X%; live-verified on current code: N/A pending ≥30 closed trades." Then only update when live evidence exists. This alone eliminates half the ambient confusion.

---

## 2. Orchestrator-Level Drift

| Item | Docs say | Code says | Files |
|---|---|---|---|
| Cycle interval | 1 hour (3600s) — CLAUDE.md §6, SSOT §6, SSOT §16 | **30 min (1800s)** | [main.py:171](../src/orchestrator/main.py#L171) |
| Trading pairs | 3 (ETH/SOL/DOGE) in most docs; 9 in others; BTC included in some | **8 live: ETH, SOL, DOGE, XRP, LINK, AVAX, SUI, ADA — BTC is commented-out** | [main.py:144-155](../src/orchestrator/main.py#L144) |
| Wrong-side force close threshold | 2 cycles (CHANGELOG v6.20) | **8 cycles (~4h)** | [main.py:173](../src/orchestrator/main.py#L173) |
| Max concurrent positions | 3 hard (CLAUDE.md Rule #3; SSOT §12 Rule #3) | **4 effective in GREEN** via `_get_effective_max_positions` when confidence ≥60 and balance ≥$60 | [main.py:179-181, 2533-2550](../src/orchestrator/main.py#L179) |
| `MIN_CONFIDENCE` (strategy gate) | 25% (SSOT §4.4) | **45%** (raised Apr 2026) | [adaptive_strategy.py:67](../src/strategies/adaptive_strategy.py#L67) |
| MAX_HOLD_BARS | 150 (SSOT old), 100 (CLAUDE.md) | **100** | [main.py:172](../src/orchestrator/main.py#L172) |
| `CONTINUATION_LOOKBACK_1H` | 5 (CHANGELOG v6.19) | **8** (v6.20 raised to 8) | [supertrend_trend.py:97](../src/strategies/supertrend_trend.py#L97) |
| Position sizing tiers | 15% / 10% / 7% (CLAUDE.md §5.2 old paragraph) | **25% / 16.7% / 11.7%** (updated §5.2 is correct) | [main.py:857-863](../src/orchestrator/main.py#L857) |
| RSI pullback threshold LONG | 45 (v6.19) | **55** (v6.20 raised) | [supertrend_trend.py:696](../src/strategies/supertrend_trend.py#L696) |
| RSI pullback threshold SHORT | 55 (v6.19) | **45** (v6.20 lowered) | [supertrend_trend.py:697](../src/strategies/supertrend_trend.py#L697) |

---

## 3. Strategy Routing Drift

[CLAUDE.md §5 "Active Strategy: SupertrendTrend ONLY"](../CLAUDE.md) — **WRONG**. Code routes three strategies:

| Regime | CLAUDE.md §5 claim | Actual routing |
|---|---|---|
| TRENDING (ADX≥18) | SupertrendTrend | ✅ SupertrendTrend |
| TRENDING (ADX<18) | NO TRADE | ✅ NO TRADE |
| RANGING (ADX≥18) | NO TRADE | ❌ **SupertrendTrend (dead-zone bridge)** |
| RANGING (ADX<18) | NO TRADE (MR disabled) | ❌ **AdaptiveTrend** (new in Sprint 2, CHANGELOG 2026-04-05) |
| VOLATILE (ADX≥15) | NO TRADE (Breakout disabled) | ❌ **BreakoutTrader** (re-enabled v6.11) |
| QUIET | NO TRADE | ✅ NO TRADE |

Also missing from CLAUDE.md §5 / SSOT §4: `adaptive_trend.py` exists ([src/strategies/adaptive_trend.py](../src/strategies/adaptive_trend.py)), `cross_asset_consensus.py` exists ([src/strategies/cross_asset_consensus.py](../src/strategies/cross_asset_consensus.py)) and applies ±10 pt confidence adjustment per-pair before execution ([main.py:554](../src/orchestrator/main.py#L554)).

SupertrendTrend's own signal cascade is also wrong in docs: SSOT §4.3 describes it as "4H flip only"; actual code is a **4-level cascade** (flip → 1H-continuation → 15m-fast → aligned-trend-RSI-pullback).

---

## 4. SSOT Internal Self-Contradiction

[docs/SINGLE_SOURCE_OF_TRUTH.md](SINGLE_SOURCE_OF_TRUTH.md) contradicts itself:

| Location | Claim A | Location | Claim B |
|---|---|---|---|
| SSOT §4.3 | "When Supertrend flips against the position, the position should be **closed immediately**" | SSOT §4.3, §6 Step 2 | "**Tighten SL to breakeven** — position stays open" |
| SSOT §1 header | Tests: 598 passing | CHANGELOG 2026-04-11 | 638 tests passing (v6.22) |
| SSOT §3 file tree header | "8 AI agent definitions" | SSOT §3 body | 9 files (8 + watchdog) |
| SSOT §1 status | "Paper trading — RESTARTING after 5 critical bug fixes (balance $5,102.70)" | .env | `BINANCE_TESTNET=false`, balance is $68 mainnet |

**Verdict:** SSOT is broken beyond partial edits. It should be archived and regenerated from code, not patched.

---

## 5. Agent Definition Drift (.claude/agents/*.md)

All 9 agent files were last written **2026-03-15** (`ls -la .claude/agents/`). Since then the bot has gone through v6.5 through v6.22 (17 version bumps). They reference:

| File | Stale reference | Reality |
|---|---|---|
| `watchdog.md` L9, L97, L124, L145 | "0.628% validated ceiling" | Ceiling is unverified on current code; closest backtest (v6.16) is 2.68% |
| `watchdog.md` L44 | `performance` CLI returns `target_gap` vs 0.628% | Tool was designed for a different strategy generation |
| `orchestrator.md`, `sentinel.md`, etc. | Describe the bot as a Claude-agent-conducted multi-agent system | Orchestrator is a direct Python class (`src/orchestrator/main.py::Orchestrator`); these agents are not driving any code path |
| All 8 non-watchdog agent stubs | 1–3 KB, no concrete tool list | Skeletons; not operationally used |

**Required action:** Either rewrite all 9 to describe *monitoring-only* duties with accurate current-state facts, OR delete the non-watchdog 8 (they're not wired). This document recommends delete-8, rewrite-watchdog — see [AGENT_PLAYBOOK.md](AGENT_PLAYBOOK.md).

---

## 6. Config File Drift

| File | Drift |
|---|---|
| [config/risk/risk_params.yaml](../config/risk/risk_params.yaml) | Still references `method: half_kelly`, `kelly_fraction: 0.5`. Orchestrator uses Kelly only as ceiling on confidence tiers ([main.py:882-896](../src/orchestrator/main.py#L882)); full method is never "half_kelly" anymore. `max_risk_per_trade_pct: 0.02` and `stop_loss.atr_multiplier: 1.5` / `max_sl_pct: 0.03` are entirely unused — actual SL is 3×ATR via `SL_TP_BY_REGIME` |
| [.env](../.env) | `MAX_CONCURRENT_POSITIONS=5` — unused; CB hardcodes 3 with dynamic +1 override |
| [.env](../.env) | Both PROD and TESTNET keys in plaintext side-by-side with `BINANCE_TESTNET=false` switch — hygiene risk |
| [config/regime/regime_params.yaml](../config/regime/regime_params.yaml) | `adx_min: 20` matches code; [was fixed v6.13 H2] |

---

## 7. Test-Count Drift

| Document | Claim | Likely real number |
|---|---|---|
| CLAUDE.md §11 | "598 tests passing" | CHANGELOG says 606 (v6.20), 623 (v6.21 before), 630 (v6.21 after), 638 (v6.22). Current is probably ≥638. Needs a `pytest --collect-only` run to confirm. |
| SSOT §1, §14 | "598 passing (~1.75s)" | Same as above |
| CHANGELOG 2026-04-11 (latest v6.22) | "638 passed, 3 warnings" | Most recent canonical value |

---

## 8. Dates & "Current" Markers

| File | Stated "Last Updated" | Truth |
|---|---|---|
| CLAUDE.md | 2026-04-08 (v3.1.0) | 14 days stale as of 2026-04-22 |
| SSOT | 2026-03-24 | 29 days stale |
| SSOT §1 bot PID | 32036, restarted 2026-03-24 | Unknown — no process running now |
| CLAUDE.md bot PID | 52685, v6.17, restarted 2026-04-08 | Unknown — no process running now |
| watchdog.md | 2026-03-15 | 38 days stale, references v3-era ceilings |

---

## 9. Persistence Drift — RESOLVED 2026-04-22 (Sprint 1)

| Item | Before Sprint 1 | After Sprint 1 |
|---|---|---|
| Single consolidated DB | `claude_quant.db` existed but `trades` table was empty — live writes went to `trade_journal.db` | ✅ TradeJournal now writes to `claude_quant.db` (orchestrator passes `db_path=self.db.db_path`) — see [SPRINT1_IMPLEMENTATION.md §B](SPRINT1_IMPLEMENTATION.md) |
| Legacy `trade_journal.db` | 17 live trades in the legacy file | ✅ Merged into canonical via `scripts/migrate_to_canonical_db.py` (INSERT OR IGNORE, idempotent); legacy file archived to `user_data/agent_state/archive/` |
| Legacy `audit_trail.db` | 95 rows in a standalone file | ✅ `DecisionAuditor` default path now points at canonical DB; migration helper copies existing rows; legacy file archived |
| `daily_state.json` | Dual-write (file + DB absent) | ✅ Primary persistence is `system_state.daily.*`; file no longer written; legacy file archived |
| `drawdown_state.json` | Only JSON (not in DB at all) | ✅ Primary persistence is `system_state.drawdown.*`; `DrawdownMonitor.attach_db` routes all writes to DB; legacy file archived |
| `trailing_stops.json` | Dual-write (DB + JSON) | ✅ JSON side-write removed from `_persist_trailing_stop_state`; DB is sole source of truth |
| `tradememory.db` | Not mentioned in orchestrator docs | Referenced in .env; MCP client code exists ([src/memory/trade_memory_client.py](../src/memory/trade_memory_client.py)); not invoked in main cycle — **still untouched** |

---

## 10. Files That Should Stop Being Trusted (deprecate or delete)

| File | Why |
|---|---|
| `docs/SYSTEM_REVIEW.md` (53KB, 2026-03-16) | Pre-v6.1 audit; 14+ versions ago; contradicts current code in dozens of places |
| `.claude/agents/{orchestrator,sentinel,market-analyst,strategy-selector,risk-manager,execution-agent,memory-agent,daily-reporter}.md` | Not driving any code path; stubs |
| `scripts/backtest.py`, `backtest_v2.py`, `backtest_v3.py` | Superseded by `backtest_v4.py` + `backtest_aggressive.py` |
| `scripts/watchdog.py` | CHANGELOG calls this "Legacy simple watchdog (replaced by Claude agent)" — decide: keep one, delete one |
| `user_data/strategies/ClaudeQuantAdaptive.py` | Freqtrade bridge; orchestrator path doesn't use it |
| `user_data/agent_state/trailing_stops.json`, `daily_state.json` | Secondary to SQLite; dual-write risk; should be either read-only historical artifacts or removed |
| Everything in `src/mcp_tools/` | MCP servers not consumed by the orchestrator cycle |

---

## 11. The 3-Step Drift-Fix Protocol

When updating this bot, always follow:

1. **Change the code.** Tests first if there's a strategy/risk impact.
2. **Update [CURRENT_STATE.md](CURRENT_STATE.md)** with the new value + file:line citation. This is the ONE doc that must stay true.
3. **Delete any now-false claim** from CLAUDE.md, SSOT, CHANGELOG, agents/*. Never leave contradictions "for context". Contradictions poison future sessions.

If you cannot complete all 3 steps, do NOT ship the code change.
