# SESSION_BOOTSTRAP.md — How to Start a Claude Session Cheaply

> **Problem:** Every new Claude session re-reads CLAUDE.md (~20K tokens) + SSOT (~40K tokens) + CHANGELOG (~40K tokens) + file tree exploration + several source files before giving a useful answer. That's ~120K tokens of context for questions that need maybe 10K.
> **Rule:** The canonical documents are [docs/CURRENT_STATE.md](CURRENT_STATE.md) and [docs/DRIFT_MAP.md](DRIFT_MAP.md). Everything else is secondary.

---

## Minimum Read Set for Any Session (load these first, in order)

1. **`docs/CURRENT_STATE.md`** — ground truth, verified against code (~5K tokens).
2. **`docs/DRIFT_MAP.md`** — what docs lie about (~3K tokens).
3. **`docs/MASTER_ROADMAP.md`** — phase + no-go items (~3K tokens).
4. **`CLAUDE.md` (immutable rules section only — §1 "The Ten Immutable Rules" + §2 "Anti-Hallucination")** — (~4K tokens).

That's ~15K tokens to be fully oriented. Do NOT read the rest of CLAUDE.md, SSOT, or CHANGELOG unless the task requires it.

---

## Task-Specific Extra Reads (ONLY if needed)

| Task type | Read also | Why |
|---|---|---|
| Strategy logic change | `src/strategies/supertrend_trend.py`, `src/strategies/adaptive_strategy.py` | Cascade detail lives here |
| Risk rule change | `src/risk/circuit_breaker.py`, `src/risk/leverage_manager.py`, `src/risk/position_sizer.py` | Hardcoded constants |
| Order execution change | `src/execution/order_manager.py` | Idempotency, conditional orders, post-only |
| Schema / persistence | `src/data/database.py` | Current tables |
| Live bug investigation | `user_data/logs/bot.log` (tail -500), `cycle_history` SQL query | Evidence |
| New telemetry | `docs/LIVE_FORENSICS_SPEC.md` | Spec for the attribution layer |
| Agent / monitoring work | `docs/AGENT_PLAYBOOK.md`, `.claude/agents/watchdog.md` | Current agent surface |
| Infra / scaling | `docs/INFRA_DECISION.md` | Recommended path |

---

## Things to Skip (unless explicitly required)

| Path | Why skip |
|---|---|
| `docs/SINGLE_SOURCE_OF_TRUTH.md` | Internally self-contradictory (see DRIFT_MAP §4). Archived. |
| `docs/SYSTEM_REVIEW.md` | 53KB, 2026-03-16, pre-v6.1. |
| Full `CHANGELOG.md` (read only the top 100 lines for most-recent-fixes context) | 40K tokens of mostly-stale history; the most-recent version is what matters |
| `user_data/strategies/*.py` | Freqtrade bridge, not on live orchestrator path |
| `src/mcp_tools/*.py` | Not consumed by the orchestrator cycle |
| `.claude/agents/{orchestrator,sentinel,market-analyst,strategy-selector,risk-manager,execution-agent,memory-agent,daily-reporter}.md` | Stubs; not driving behavior |
| `scripts/backtest.py`, `backtest_v2.py`, `backtest_v3.py` | Superseded by `backtest_v4.py` + `backtest_aggressive.py` |
| `src/strategies/mean_reversion.py`, `trend_follower.py`, `scalper.py` | Not routed by `AdaptiveStrategy` |

---

## Token-Saving Etiquette

1. **Never read more than 2000 lines at a time** unless the file is smaller. Use offset/limit reads against CHANGELOG, main.py, SSOT.
2. **Prefer `grep -n`** for "does X exist" questions over reading whole files.
3. **Do not re-read a file you've already summarized** unless you've made a change or you suspect drift. Your scratchpad is the source.
4. **Do not build a new "what does this bot do" summary** if `docs/CURRENT_STATE.md` already has it. Cite CURRENT_STATE with file:line links instead.
5. **Ask the user "is CURRENT_STATE.md still fresh?" before re-verifying** when the task is small. It's cheaper to trust-and-verify-on-write than to re-read everything.

---

## First-Message Template for a New Session

When the user opens a new session, the Claude response SHOULD look like:

> I loaded `docs/CURRENT_STATE.md` and `docs/DRIFT_MAP.md`. Current bot state: 8 pairs on mainnet, 30-min cycles, SupertrendTrend cascade + AdaptiveTrend (RANGING<18) + BreakoutTrader (VOLATILE≥15), MIN_CONFIDENCE=45, CB hardcoded 3/2/1 positions with dynamic +1 in GREEN (effective cap 4). What do you want to work on?

Not:
> Let me read CLAUDE.md, SSOT, CHANGELOG, the orchestrator file, the strategies folder, and the risk folder to understand the project... [50K tokens in]

---

## When You MUST Re-Verify

Re-verify `CURRENT_STATE.md` against code ONLY when:
- User says "what's the current X" and you notice CURRENT_STATE is older than 7 days.
- You are about to make a code change in one of the files CURRENT_STATE cites.
- The user reports a behavior that conflicts with CURRENT_STATE.
- A Phase-1/2/3 task from MASTER_ROADMAP is being executed.

In those cases, update CURRENT_STATE.md immediately after verifying. If you don't update it, the next session pays for your laziness.

---

## The One Command to Rule Them All

If you're reading this in a new session and you don't want to read anything else, just run:

```bash
grep -n "TRADING_PAIRS\|CYCLE_INTERVAL\|MIN_CONFIDENCE\|MAX_HOLD_BARS\|DYNAMIC_POS\|WRONG_SIDE" \
  src/orchestrator/main.py src/strategies/adaptive_strategy.py \
  src/strategies/supertrend_trend.py
```

That's the 30-line snapshot of the bot's current soul.
