# AGENT_PLAYBOOK.md — Roles, Arbitration, Anti-Hallucination

> **Date:** 2026-04-22
> **Context:** 9 agent-markdown files currently exist in `.claude/agents/`. Only `watchdog.md` is being actively invoked (via `@watchdog`). The other 8 (`orchestrator`, `sentinel`, `market-analyst`, `strategy-selector`, `risk-manager`, `execution-agent`, `memory-agent`, `daily-reporter`) are stubs from 2026-03-15 that never drove live code.
> **Recommendation:** Consolidate to 4 actively-used roles. Delete or rewrite stubs. Describe every role with (1) when to invoke, (2) the exact tools it uses, (3) the arbitration rule when two agents disagree, (4) anti-hallucination constraints.

---

## Active Agent Roster (target state)

### 1. `watchdog` (keep, rewrite)
Live monitoring of the running bot. Invoked by human `@watchdog` or future scheduled cron. Can run completely autonomously — all it does is read-only observation + alerts.

**When to invoke**
- Every 15–30 minutes during live trading, as a check-in.
- On-demand after any manual intervention or suspected anomaly.
- After a cycle stall alert fires.

**Tools it uses**
- `Bash` to run `scripts/watchdog_tools.py {health|logs|performance|mistakes|market}`.
- `Read` on `user_data/logs/bot.log`.
- SQL read-only on `user_data/claude_quant.db` (via `sqlite3` CLI).
- NO order placement. NO config mutation. NO file edits.

**Arbitration rule**
- Watchdog never makes decisions that change live behavior. Its output is advisory. If it disagrees with the orchestrator's actual decision, it reports; the orchestrator wins until a human overrides.

**Anti-hallucination constraint**
- Every claim includes a data timestamp. If data is unavailable, emit `UNKNOWN`, never synthesize.
- Numeric thresholds (win-rate gates, daily-rate targets) must be quoted from `docs/CURRENT_STATE.md` — NOT hardcoded in the agent prompt.

---

### 2. `council-of-five` (new, reusable — see `.claude/agents/council_of_five.md`)
Multi-perspective critic used before any non-trivial change is committed. Five voices: exchange microstructure, paranoid auditor, deletionist, forensic data engineer, scale predator. Three rounds (diagnose → cross-critique → final). Final arbiter merges only strong-evidence consensus.

**When to invoke**
- Before merging a roadmap phase.
- Before enabling/disabling a strategy.
- Before adding/removing a safety gate.
- Before any `config/` or `circuit_breaker.py` edit.

**Anti-hallucination constraint**
- Every voice must cite a file:line or a specific trade_id/date when making a claim.
- Any voice that produces an opinion without evidence is automatically disregarded by the arbiter.

---

### 3. `drift-sentinel` (new)
Dedicated agent for running the drift-prevention protocol from [DRIFT_MAP.md](DRIFT_MAP.md).

**When to invoke**
- After every non-trivial code change.
- Weekly, unconditionally.

**Tools**
- `grep` to find stale strings (`0.628%`, `1.149%`, `1.397%`, old pair lists, old thresholds).
- `Read` on `docs/CURRENT_STATE.md` to compare vs current code.
- `Edit` to update docs (never to update code).

**Arbitration**
- If code disagrees with docs, drift-sentinel updates docs to match code.
- If code disagrees with `CLAUDE.md` Immutable Rule, drift-sentinel escalates to human — the code is wrong, not the rule.

---

### 4. `forensic-analyst` (new — pairs with LIVE_FORENSICS_SPEC.md)
Interprets `decision_log` + `trades` attribution tables. Asked questions like "which cascade level loses money in volatile regime".

**Tools**
- Read-only SQL against `user_data/claude_quant.db`.
- Write to `docs/reports/YYYY-MM-DD-attribution-*.md`.

**Arbitration**
- Recommends; does not mutate code or config.
- Any recommendation must include the SQL query that supports it, pasted verbatim.

**Anti-hallucination constraint**
- Never produce a "typical" or "historical" number without a specific query + date range.
- If the data does not exist (attribution gap from Phase 1), say so and stop.

---

## Agents to Delete (unless rewritten to a concrete role)

These 8 stubs in `.claude/agents/` do not correspond to anything in the live code path:

| File | Claimed role | Reality | Action |
|---|---|---|---|
| `orchestrator.md` | Master coordinator | The orchestrator is a Python class, not an agent | Delete |
| `sentinel.md` | CB enforcement | Hardcoded in `circuit_breaker.py`; agent does nothing | Delete |
| `market-analyst.md` | Technical analysis | `IndicatorEngine` + `RegimeDetector` do this | Delete |
| `strategy-selector.md` | Regime-to-strategy mapping | `AdaptiveStrategy.select_strategy` does this | Delete |
| `risk-manager.md` | Sizing + approval | `PositionSizer`, `LeverageManager`, `SanityChecker` do this | Delete |
| `execution-agent.md` | Order management | `OrderManager` does this | Delete |
| `memory-agent.md` | Trade journaling | `TradeJournal` + `DatabaseManager` do this | Delete |
| `daily-reporter.md` | P&L reporting | `DailyPnLCalculator` + `ReportGenerator` do this | Delete |

**Why delete, not just ignore:** Their existence signals "this project is agent-conducted", which is misleading. Future Claude sessions will burn tokens reading them expecting guidance and find empty shells.

---

## Arbitration Rules (Universal)

These apply across all agents:

1. **Code wins over docs.** If an agent's instruction conflicts with the code's current behavior, code is truth; the agent instruction must be updated.
2. **Hardcoded safety wins over every agent.** No agent can justify violating the Ten Immutable Rules.
3. **Live evidence wins over backtest evidence.** When a backtest and a live-trade log conflict, trust the live log.
4. **Watchdog never executes.** Only `Orchestrator._execute_signal()` places orders. Every other agent is advisory.
5. **Multi-agent disagreement → no action.** If two advisors disagree and there is no live-evidence tie-breaker, the bot must NOT act until a human decides.
6. **No agent edits `circuit_breaker.py`, `leverage_manager.py`, or `.env` prod keys without an explicit human approval token** (a flag in the agent invocation).

---

## Anti-Hallucination Operating Rules (Universal)

From CLAUDE.md §2, refined and made concrete for agent use:

1. **Ground everything.** Claims that aren't grounded in (a) a file:line, (b) a trade_id, (c) a timestamped log line, or (d) official Binance docs are treated as speculation.
2. **Confidence < 10/10 = research only.** Do not execute, do not commit, do not write code. Produce a "what I know / what I need / minimal experiment" brief instead.
3. **Forbidden phrasing for live decisions.** "Guaranteed", "the price will", "certainty", "impossible". Replace with "evidence suggests", "probability of", "indicator shows".
4. **If data is missing, say UNKNOWN.** Never synthesize a number from "feel".
5. **Every agent response to a numeric question must include its source.** Example: "Max leverage = 10 (circuit_breaker.py:28)". No sources = no answer.
6. **Timestamps on all data.** A price is useless without a timestamp. A regime is useless without a cycle_id.
7. **No "based on typical", "based on usual", "typically".** Only specific dates, ranges, or queries.

---

## New-Agent Creation Checklist

Before adding a new `.md` file to `.claude/agents/`, the author must answer (and paste the answers into the agent file):

1. What SPECIFIC code path does this agent drive, observe, or replace?
2. Which existing agent's tools does it overlap with? (If 100% overlap → merge, don't create.)
3. When the orchestrator and this agent disagree, who wins and why?
4. What's the hallucination mitigation specific to this agent's domain?
5. What's the DELETE criterion? (i.e., "delete this agent if ever X becomes true")

No answers → no merge.

---

## The One-Line Version

**Agents observe; the orchestrator decides; circuit breakers halt. No agent trades.**
