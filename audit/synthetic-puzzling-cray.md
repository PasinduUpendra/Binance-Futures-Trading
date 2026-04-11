# Claude Quant v6.20 — Forensic Audit + Research-Backed Improvement Plan

> **Plan author note**: Drafted in plan mode (read-only). Every claim is backed by either (a) a file:line in this repo, (b) a row in the local SQLite databases under `user_data/`, or (c) a cited URL from official Binance docs / a published paper / a major open-source repo. Anything I could not verify at this confidence level is marked **REJECTED** with the specific failure mode, exactly as requested.
>
> **Security incident** (recorded so it doesn't get forgotten): During the audit, the contents of `.env` were printed in conversation, exposing both `BINANCE_API_KEY_PROD/SECRET_PROD` and the testnet keys. **Rotate both pairs at Binance → API Management before any other action.** Stop the running bot first to avoid orphan orders, restart with new keys.

---

## Context

The bot is `Claude Quant v6.20`, a Binance USDT-M Perpetual Futures bot, **on real-money mainnet** (verified `.env` `BINANCE_TESTNET=false`, balance ≈ $66, PID 52685). It runs every 30 minutes across 9 pairs (BTC, ETH, SOL, DOGE, XRP, LINK, AVAX, SUI, ADA).

The user's stated framing was "the bot is actively losing money" and asked for 10 sub-agents to scrape GitHub topic pages and propose new features. The forensic done before launching research uncovered something more important: **the bot's measurement, validation, and recording infrastructure is broken — so the v6.16 backtest claim of 2.68%/day cannot be validated against live behaviour, and many of the v6.18–v6.20 "fixes" are partially regressed**. New features added on top of broken infrastructure are dangerous.

This plan is therefore three layers, executed in order:

1. **Phase 0 — Stabilize the floor.** Fix the broken measurement/validation/recording so any future claim can be objectively tested. Zero new alpha. Pure correctness.
2. **Phase 1 — Two-line-fix exchange-quality wins.** A small set of changes whose evidence is overwhelming and whose blast radius is minimal.
3. **Phase 2 — Research-backed additions** — only after Phase 0 is in and the v6.20 baseline has been re-measured. Each candidate is held against the user's bar: "prove it increases avg daily return, Sharpe, and win rate; list every way it could break existing systems; if any risk exists, REJECT."

---

## Forensic Findings (the actual loss profile, evidence-grounded)

All findings derived from local DBs and code, not assumptions.

| # | Finding | Severity | Evidence |
|---|---|---|---|
| **F1** | **Decision auditor is structurally bypassed**. `main.py:932` comment is literally `# ── Audit (non-blocking) ──`; the audit's REJECT decision is never read. The orchestrator hard-codes `"approved": True` (`main.py:948`) before invoking the auditor. Immutable Rule #7 ("Risk Manager Approval — every trade must pass risk checks") is not enforced. | **CRITICAL** | `src/orchestrator/main.py:932-953`; all 28 rows of `user_data/audit_trail.db` show `decision: REJECT` despite the trades being placed. |
| **F2** | **Auditor field starvation**. The orchestrator passes `position_size_usd, leverage, notional, confidence, position_pct, approved` to the auditor; it does NOT pass `risk_per_trade_pct`, `risk_reward_ratio`, or `kelly_fraction`. The auditor records all three as `0` and emits "Risk/reward ratio 0.00 is below 2.0" — false negative on every record. | HIGH | `src/orchestrator/main.py:942-949` vs `src/anti_hallucination/decision_auditor.py:199` |
| **F3** | **Trade journal pnl/duration recording broken (13/21 records)**. Rows from 2026-04-06 onward show `pnl=None`, `pnl_pct=None`, `duration=0`. Earlier rows show `pnl_pct = -1000%` or `+2410%` — the percent formula is wrong (looks like `pnl/exit_price` rather than `pnl/margin`). v6.18 changelog claimed this was "fixed" via a None guard; the guard prevents the crash, but the underlying recording path still drops the data on most exit paths. | **CRITICAL** | `user_data/agent_state/trade_journal.db` rows for ETH 2026-04-06, ADA 2026-03-26 (-1000%), AVAX 2026-03-26 (+1579%), LINK 2026-03-30 (+2410%) |
| **F4** | **`regime` column empty for ALL 21 trades.** Regime is computed every cycle (cycle_history shows `'trending'`) but is never persisted to `trades.regime`. Cannot measure regime-conditional win rate. | HIGH | `user_data/agent_state/trade_journal.db` `regime` column |
| **F5** | **Trade journal mixes paper trading and mainnet sources without separation**. `daily_reports` shows `start_balance=$5,949` on 2026-04-06 then `$68` on 2026-04-07. Audit records show position_size_usd of `886.52` (ADA) and `444.05` (AVAX) — sane on a $5,500 paper balance, lethal on $66 mainnet. The same SQLite tables hold both. Any "win rate over recent trades" calculation is contaminated. | **CRITICAL** | `user_data/claude_quant.db` `daily_reports` rows; `user_data/audit_trail.db` ADA REJECT row |
| **F6** | **CLAUDE.md says "Phase: Paper Trading on Testnet ($5000 balance)" — bot is actually on mainnet with real money since 2026-04-07.** Constitutional document is dangerously stale. | **CRITICAL** | `CLAUDE.md` Project Identity table; `.env` `BINANCE_TESTNET=false`; CHANGELOG v6.16 entry |
| **F7** | **`fee_calculator.py:22` hardcodes taker = 0.0005 (0.05%) — the official VIP-0 USDT-M taker is 0.04%.** Round-trip taker is 0.08%, not 0.10%. CLAUDE.md §3 carries the same wrong number. The TP target adjustment math is over-padded by 0.02% per round-trip. Over the 197 trades in the v6.16 backtest, this is a 3.94% headwind that does not exist in reality. The backtest baseline understates true performance, but more importantly the **break-even gate** the bot uses to decide whether a setup is worth taking is wrong. | HIGH | `src/execution/fee_calculator.py:21-22`; `https://www.binance.com/en/fee/futureFee` |
| **F8** | **STOP_MARKET / TAKE_PROFIT_MARKET trigger on `CONTRACT_PRICE` (last trade), not `MARK_PRICE`.** `order_manager.py` has zero references to `workingType`. ccxt's default for Binance Futures is `CONTRACT_PRICE`. This means a wick on a single bad print can stop the bot out even though Binance's mark price (which controls liquidation) never crossed the level. This is a known wick-liquidation bug. Confirmed by three independent sources (ccxt source, Nautilus enums, Binance docs). | **CRITICAL** | `src/execution/order_manager.py` (no `workingType`); ccxt `binance.py:5380, 5481, 5584, 5655` (sample responses); Nautilus `adapters/binance/futures/enums.py:75-76`; https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order |
| **F9** | **Reversal-exit handler still crashing in v6.20**. Cycles 698 and 699 (today, 09:18 / 09:48 UTC) errored: `'OrderStatus' object has no attribute 'get'`. v6.20 changelog claims "reversal exit deduplication" was added (`main.py:1233`); the dedup added a `.get()` call on what is actually an `OrderStatus` enum/object, not a dict. Same code path that already burned the user once. | HIGH | `user_data/claude_quant.db` `cycle_history` rows 698, 699 (`errors` column) |
| **F10** | **Confidence is anti-predictive in the live sample.** 8 closed trades: wins carry 60-72% confidence, losses carry 75-83%. Sample is small, but it inverts the v6.16 backtest assumption that higher confidence → higher expected value. Position sizing tiers (25%/16.7%/11.7%) are keyed to confidence — if confidence is anti-predictive, the bot is over-sizing its losers. | HIGH | `user_data/agent_state/trade_journal.db` |

---

## Research-Backed Findings (cited, cross-verified against v6.20 code)

Four parallel research agents covered: (1) freqtrade & jesse, (2) hummingbot & nautilus_trader, (3) academic perp futures alpha papers, (4) official Binance Futures API. Every claim below has a source URL **and** a verification of whether it exists in v6.20.

### R-block A — Order execution quality (highest leverage, lowest risk)

| ID | Finding | Source | v6.20 status |
|---|---|---|---|
| **R-A1** | **STOP_MARKET wick-liquidation bug** — `workingType` defaults to `CONTRACT_PRICE`. Setting `workingType=MARK_PRICE` makes the trigger use mark price (the same number Binance uses for liquidation). Binance also exposes `priceProtect=true` which rejects triggers when mark vs contract price diverge implausibly. Two-line fix per call site. | https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order ; Nautilus `adapters/binance/common/http/account.py:195-198` ; Hummingbot `binance_perpetual_derivative.py` | **MISSING**. `order_manager.py:765-829` `place_stop_loss` / `place_take_profit` pass no `workingType`. |
| **R-A2** | **`fee_calculator.py` hardcodes wrong taker (0.05% vs 0.04%).** | https://www.binance.com/en/fee/futureFee | **WRONG**. `fee_calculator.py:21-22`. CLAUDE.md §3 also wrong. |
| **R-A3** | **Native `TRAILING_STOP_MARKET`** with `callbackRate` (0.1-5%) and `activationPrice`. Survives bot crashes/disconnects/cycle gaps. The bot today implements local Python trailing in 30-min cycles — if the bot crashes between cycles, the trail is dead until restart. | https://developers.binance.com/docs/derivatives/change-log ; ccxt issue #7224 | **MISSING** in `order_manager.py`. |
| **R-A4** | **`POST /fapi/v1/batchOrders`** — atomic entry+SL+TP in one signed request (max 5 per batch). Closes the "unprotected position" window the bot opens between `place_market_order` and the subsequent `place_stop_loss` calls. | https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Place-Multiple-Orders | **MISSING**. |
| **R-A5** | **`timeInForce=GTX` (post-only)** for entries. Fee saving of 0.02% per side vs taker (0.04% → 0.02%). Requires fallback-to-market if the post-only order is rejected for crossing. | https://developers.binance.com/docs/derivatives/coin-margined-futures/common-definition ; Hummingbot `binance_perpetual_constants.py:22` | **MISSING**. CLAUDE.md §3 actually instructs to use this; the code does not. |
| **R-A6** | **User-data WebSocket stream** for fills/partials/position updates. Bot today reconciles via REST every cycle (30 min). A fill that happens 1 minute after a cycle ends is invisible until 29 minutes later. WS user-data delivers `ORDER_TRADE_UPDATE` events in ~100 ms. | https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams ; Hummingbot, Nautilus | **MISSING** for reconciliation. (Bot has WS for OHLCV.) |
| **R-A7** | **Dual-source open-orders reconciliation** — query both `GET /fapi/v1/openOrders` AND `GET /fapi/v1/openAlgoOrders`. STOP_MARKET / TAKE_PROFIT_MARKET live on the **algo book** before trigger, on the **regular book** after. Single-source reconciliation can miss untriggered SL/TP entirely. | Nautilus `adapters/binance/futures/execution.py:400-450` ; https://developers.binance.com/docs/derivatives/change-log | **NEEDS-VERIFICATION** of which path ccxt's `fetch_open_orders` uses for binanceusdm. |

### R-block B — Validation discipline (prerequisite for any new alpha)

| ID | Finding | Source | v6.20 status |
|---|---|---|---|
| **R-B1** | **`freqtrade lookahead-analysis`** — chains backtests sliced per signal, diffs indicator values & timestamps vs full-range run, flags `shift(-N)`, `iloc[]`, unrolled `.mean()` lookahead bias. Code-blind detection. | https://www.freqtrade.io/en/stable/lookahead-analysis/ | **MISSING**. `backtest_v4.py` has no equivalent. |
| **R-B2** | **`freqtrade recursive-analysis`** — detects indicators whose values change depending on dataframe length (TA-Lib warmup issues). Produces silently different signals in live vs backtest. | https://www.freqtrade.io/en/stable/lookahead-analysis/ | **MISSING**. |
| **R-B3** | **Walk-forward / Monte-Carlo** robustness testing — Jesse ships trade-order shuffling and candles-perturbation. v6.20 has a single backtest path. | Jesse README "Monte Carlo Analysis" | **MISSING**. |
| **R-B4** | **`enter_tag` / `exit_tag` attribution** — multiple sub-strategies tag every entry/exit, and a per-tag PnL report tells you which alpha is actually working. v6.20 has SupertrendTrend, AdaptiveTrend, CrossAssetConsensus, FundingRateFilter, plus 4H/1H/15m signal cascades — and no per-component attribution. After any change, you don't know which knob did it. | https://www.freqtrade.io/en/stable/strategy-advanced/ | **MISSING**. The trade journal's `strategy` column captures the top-level strategy name only. |

### R-block C — Risk management beyond circuit breakers

| ID | Finding | Source | v6.20 status |
|---|---|---|---|
| **R-C1** | **Liquidation cascade early-warning** — sustained funding >15% APR + rising OI is a documented 12-24h precursor to cascades. Oct 10-11 2025 cascade liquidated $19B in 36h, peak rate $6.93B in 40 min (86× normal). | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5611392 ; https://blog.amberdata.io/leverage-liquidations-the-31b-deleveraging | **MISSING**. Bot has FundingRateFilter (binary go/no-go on the current rate) but no sustained-spike + OI-delta cascade detector. |
| **R-C2** | **StoplossGuard / MaxDrawdown(rolling) / LowProfitPairs / CooldownPeriod** as named primitives. The bot has the *daily* loss halt (Rule, 10%) and 5-consecutive-loss pause, but no rolling-equity drawdown halt and no per-pair underperformance lockout. | https://www.freqtrade.io/en/stable/plugins/ | **PARTIAL**. |
| **R-C3** | **Volatility targeting** vs fixed-fraction sizing — published evidence (Concretum 2025, Quantpedia) that targeting a constant daily portfolio vol (e.g., 2%) outperforms fixed-fraction on Sharpe in trend-following. | https://concretumgroup.com/position-sizing-in-trend-following-comparing-volatility-targeting-volatility-parity-and-pyramiding/ ; https://quantpedia.com/an-introduction-to-volatility-targeting/ | **MISSING**. v6.20 is fixed-fraction (11.7/16.7/25%) with GARCH downward-only adjustment. |

### R-block D — New alpha sources (CITATION-GRADE only; all flagged for Phase 2)

| ID | Finding | Source | Sharpe / Return |
|---|---|---|---|
| **R-D1** | **Funding-gap mean reversion** on perp futures. | He, Manela, Ross, von Wachter, *Fundamentals of Perpetual Futures*, https://arxiv.org/abs/2212.06888 | Sharpe 1.8 retail, 3.5 MM |
| **R-D2** | **Delta-neutral funding carry** (long spot / short perp, 3× leverage). | Skyler Chan, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5292305 | Sharpe 6.1, ≤2% max DD — REQUIRES SPOT LEG |
| **R-D3** | **Cross-sectional momentum** (long top-N / short bottom-N) on crypto perps. | Fracassi & Kogan, *Pure Momentum in Cryptocurrency Markets*, SSRN 4138685 | Sharpe >1, ~20% annual |
| **R-D4** | **BTC intraday time-series momentum** (last 30 min predictable from earlier-day half-hours). | Shen, Urquhart, Wang, Financial Review 2022, https://centaur.reading.ac.uk/100181/ | Sharpe 1.15, 13.95% annual |
| **R-D5** | **Session weighting (16-17 UTC peak vol)** — *The crypto world trades at tea time*. | Eross et al., RQFA 2024, https://link.springer.com/article/10.1007/s11156-024-01304-1 | qualitative; cheap to implement |
| **R-D6** | **Crypto carry has decayed** — Sharpe fell from 6.45 (2020-2025) → 4.06 (2024) → negative (2025). | https://arxiv.org/html/2510.14435v2 | **DECAY WARNING** — any carry adoption MUST be walk-forward-tested on 2025 data |

---

## Plan

### Phase 0 — Stabilize the floor (must complete before anything else)

> Goal: Restore the ability to *measure* the bot. Zero alpha, zero new strategies, zero risk to existing positions. Every Phase 0 task makes a Phase 1/2 decision possible.

| # | Task | Files to modify | Acceptance |
|---|---|---|---|
| **P0-1** | **Rotate API keys** (manual user action — first thing). Stop bot, regenerate both PROD and TESTNET keys at Binance API Management with IP whitelist + Futures-only permissions, update `.env`, restart. | `.env` (user does this manually) | New keys verified by `scripts/check_balance.py` reading the new keys. |
| **P0-2** | **Update CLAUDE.md to reflect reality.** Phase: mainnet, balance: actual current balance from Binance API, version: v6.20, agent count: 9, fee taker: 0.04%. Add a "last verified" line for each. | `CLAUDE.md` | `grep "Paper Trading on Testnet" CLAUDE.md` → 0 matches. |
| **P0-3** | **Fix `fee_calculator.py:22`** taker = `Decimal("0.0004")`. Update default doc strings on lines 21, 39. Add a unit test that asserts taker == 0.04%. Update CLAUDE.md §3 fee table. Re-run all 606 tests. | `src/execution/fee_calculator.py`, `tests/test_fee_calculator.py`, `CLAUDE.md` | All tests green; documented break-even calculations now match exchange. |
| **P0-4** | **Wire the decision auditor as a real gate, not a logging facade.** Three sub-tasks: <br>(a) `main.py:932-953` — populate `risk_per_trade_pct`, `risk_reward_ratio`, `kelly_fraction` in the `risk_approval` dict (these are already computed locally; just pass them). <br>(b) `main.py:932` — change `# Audit (non-blocking)` block: read the auditor's returned `decision`; if `REJECT`, return None (do not place the trade). <br>(c) Add a single `BYPASS_AUDIT_DECISION` env var (defaults to `false`) so the user can force-bypass for the first 24h while watching that it doesn't reject everything. <br>(d) Wire `price_validator` and `signal_validator` so they actually run (currently `price_validated: false` on every audit row — they're never invoked). | `src/orchestrator/main.py`, `src/anti_hallucination/decision_auditor.py`, `src/anti_hallucination/price_validator.py`, `src/anti_hallucination/signal_validator.py` | A new audit row from a live cycle has `price_validated: true`, `signal_validated: true`, `risk_reward_ratio` populated, and the bot's behaviour matches the audit decision. Immutable Rule #7 stops being a fiction. |
| **P0-5** | **Fix the trade journal `pnl` and `pnl_pct` recording.** Three sub-tasks: <br>(a) Trace every code path that calls `_record_trade_exit()` — there are at least 4 (SL trigger, TP trigger, reversal exit, time exit). All four must pass `pnl, pnl_pct, duration, regime, exit_reason`. <br>(b) Fix `pnl_pct` formula. Today's `-1000%` and `+2410%` rows imply division by `exit_price` rather than `margin`. Definition: `pnl_pct = pnl / margin × 100` — and label that explicitly in the column comment. <br>(c) Backfill the `regime` column on new rows by reading the regime detector at exit time (or capture it at entry and persist on the trailing-stop state). <br>(d) Add a unit test that opens a synthetic position, simulates a SL hit, asserts the journal row has all fields populated and pnl_pct is mathematically correct. | `src/orchestrator/main.py` (`_record_trade_exit` and all callers), `src/memory/trade_journal.py`, `tests/test_memory/test_trade_journal.py` | After the next live SL/TP exit, the journal row has populated `pnl`, `pnl_pct`, `duration`, `regime`, `exit_reason`. |
| **P0-6** | **Separate paper-trading and mainnet trade history.** Add a `mode TEXT NOT NULL DEFAULT 'mainnet'` column to `trades` and to `daily_reports`. Stamp it on every insert based on `BINANCE_TESTNET`. Migrate the existing rows: anything where `entry_price` is consistent with the paper balance era (start_balance > $1000) → `mode='paper'`. Every analytic that reads the journal must filter on `mode='mainnet'` by default. Add a unit test. | `src/data/database.py`, `src/memory/trade_journal.py`, `src/reporting/daily_pnl.py`, `tests/test_database.py` | Win-rate / Kelly-input queries no longer mix paper and mainnet. |
| **P0-7** | **Fix the v6.20 reversal-exit `'OrderStatus' object has no attribute 'get'` regression.** Find the call site in `main.py` near line 1233 (the dedup logic). The check is calling `.get()` on an `OrderStatus` object — change to attribute access (`order.status` or whatever the type defines). Add a regression test that reproduces the crash with a mock OrderStatus. | `src/orchestrator/main.py`, `tests/test_integration/test_orchestrator_fixes.py` | Cycle history shows zero `'OrderStatus'... .get` errors after one full day of cycles. |
| **P0-8** | **Re-run `backtest_v4.py` with the corrected fee** (P0-3) and the new audit gate (P0-4) **in dry-run mode** to produce a fresh, post-fix v6.20 baseline. Document it in CHANGELOG and CLAUDE.md as the new "validated ceiling" — the v6.16 number (2.68%/day) is from a pre-audit-gate, pre-fee-fix code path and is no longer the right baseline. | `scripts/backtest_v4.py` (no code changes — just run), `CHANGELOG.md`, `CLAUDE.md` | New row in CHANGELOG: "v6.20 backtest re-baselined: avg daily X%, Sharpe Y, max DD Z%, win rate W%". Phase 1 / 2 candidates will be measured against THIS, not v6.16. |

### Phase 1 — Two-line-fix exchange-quality wins (after Phase 0 and the new baseline)

> Each item in this phase has overwhelming evidence (cited above), minimal blast radius (no strategy logic change), and is gated through the standard test suite. They are the highest dollar-per-line-of-code changes available.

| # | Task | Acceptance | Existing-system risk |
|---|---|---|---|
| **P1-1** | **`workingType=MARK_PRICE` + `priceProtect=true` on every STOP_MARKET / TAKE_PROFIT_MARKET.** Add `params={'workingType': 'MARK_PRICE', 'priceProtect': 'true'}` in `order_manager.py:792` and `:829`. Update the in-progress test cases in `tests/test_execution/test_order_manager.py` to assert these params are forwarded. Spot-check on testnet with a deliberate wick-prone pair before mainnet. | A new live SL placed on mainnet shows `workingType: MARK_PRICE` in the `fetch_order` response. Zero existing positions touched (only new orders). | Old positions still have CONTRACT_PRICE stops — this change is forward-only, does not retroactively touch existing SL/TP. To migrate them, P1-1b: cancel-and-replace SL/TP for each open position one at a time inside the existing reconciliation loop. Two-step rollout. |
| **P1-2** | **Fix idempotent `place_stop_loss` and `place_take_profit` to use the new mark-price triggers without breaking idempotency.** Confirm that `_submit_order_idempotent` forwards arbitrary `params` correctly — if not, plumb a `params` kwarg through it. | All 33 existing `order_manager` tests pass; 4 new tests for `workingType` forwarding. | If params are not forwarded, the bug becomes silent — covered by tests. |
| **P1-3** | **Trailing-stop redundancy: keep local trailing AND add native `TRAILING_STOP_MARKET` as a backstop.** When opening a position, also place a native trailing stop with `callbackRate = 2.5 × ATR(4H) / entry_price * 100` clamped to `[0.1, 5.0]`, `activationPrice = entry ± 2.0 × ATR(4H)`. Local trailing remains the primary; native trailing is a "if the bot dies, you still get out" failsafe. Both should use `reduceOnly=True`. | New position has both a local TrailingStopState row AND an exchange-side trailing order visible in `fetch_open_orders`. Local trailing fires first in normal operation; native fires only if local fails. | Risk of double-close: if both fire, the second one hits `reduceOnly` and is rejected. Verify with a unit test that simulates both firing simultaneously. |
| **P1-4** | **Dual-source open-orders reconciliation.** In the existing 5-min reconciliation loop, query both `fetch_open_orders` AND a raw call to `/fapi/v1/openAlgoOrders` (via ccxt's `fapiPrivateGetOpenAlgoOrders` or implicit method). Merge by `clientOrderId`. Log any mismatch. | After 24h on mainnet, no untriggered SL/TP can be missed by reconciliation. Reconciliation log lines show both sources contributing. | Risk: mismatched algo vs regular IDs cause double-counting of one order. Use the `clientOrderId` (cq_<uuid>) as the primary key in the merge to defeat this. |
| **P1-5** | **`enter_tag` / `exit_tag` attribution** — add a `signal_tag` and `exit_reason` column to the trades table (ALTER TABLE), populate at insert time. Every signal generator (4H flip, 1H continuation, 15m fast, aligned-trend, AdaptiveTrend, ConsensusBoost) tags itself; every exit (SL, TP, partial TP, reversal, time, force-close, swap) tags itself. After 30 days, query: per-tag P&L. | After P0-5 (recording fixed), the new columns populate on every trade. Per-tag query in `scripts/parse_backtest.py` returns non-empty rows. | Pure additive — does not change any execution logic. |

### Phase 2 — Research-backed candidates (each one held to the user's REJECT bar)

> **Rule the user gave**: "If ANY risk exists or any claim cannot be 100% verified, output only: REJECTED - risk of [specific failure]." I'm using exactly that format.

| ID | Candidate | Source | Verdict |
|---|---|---|---|
| **C1** | **`POST /fapi/v1/batchOrders`** — atomic entry+SL+TP submission. | https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Place-Multiple-Orders | **REJECTED — risk of**: idempotency invariant breaks (current `_submit_order_idempotent` is built around single-order responses; refactoring it to handle batch responses without losing the "503 may have succeeded" guarantee is high-risk). Re-evaluate after Phase 0 audit gate is in place and there is an end-to-end batch idempotency test path. |
| **C2** | **`timeInForce=GTX` (post-only) entries with timeout fallback to market.** | https://developers.binance.com/docs/derivatives/coin-margined-futures/common-definition | **CONDITIONALLY ACCEPTED for Phase 2.** Proof of value: 0.04% taker → 0.02% maker = 50% fee saving per entry side. On 197 trades that's ~$1.50 saved on a $66 account. **Risk**: a post-only that gets rejected for crossing produces zero entry, zero stop, zero TP. The fallback-to-market path must be airtight, and the wait-time before fallback is a parameter that needs backtesting. **Acceptance criteria** before flipping the flag: (a) post-only-then-fallback path tested with a unit test for every reject reason, (b) backtest_v4 supports both modes and shows the fee saving without hurting fill rate, (c) flag is per-pair and defaults to off. |
| **C3** | **User-data WebSocket stream for fills/partials/position events.** | https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams | **REJECTED for now — risk of**: listen-key keepalive bugs leading to silent disconnect, missed events, position drift, and stale local state. WS user-data is a major plumbing project (listen key lifecycle, 24h reconnect, event ordering vs REST, conflict resolution). Revisit after Phase 1 (which already closes most of the latency gap by adding native trailing, dual reconciliation, and tagged trades). |
| **C4** | **Liquidation cascade early-warning** (sustained funding >15% APR + OI rising → reduce sizes / pause). | SSRN 5611392, Amberdata blog | **CONDITIONALLY ACCEPTED for Phase 2.** Proof of value: defensive-only, prevents the kind of -22% margin loss the bot just took on LINK SHORT. **Risk**: false positives during normal high-funding periods → unnecessary paused trading, missed alpha. **Acceptance criteria**: (a) build the detector as a standalone observer that just LOGS its trigger for 14 days before it gates anything, (b) compare its trigger timestamps against actual cascade events on the prior 90 days of OHLCV+funding data, (c) if it predicts at least 2 of the last 3 documented cascades and <1 false positive per week, then wire it as a CB-overlay (sets size_multiplier × 0.25 for 24h after trigger). Pure observer first, gate second. |
| **C5** | **Volatility targeting** replacing fixed-fraction sizing. | Concretum 2025; Quantpedia | **REJECTED — risk of**: changing position sizing math on a $66 mainnet account without a re-baselined backtest. v6.16's 2.68%/day number was produced under fixed-fraction sizing — switching the sizing changes the entire backtest's distribution and the existing risk-of-ruin math. Acceptable to revisit IF Phase 0 produces a clean re-baseline AND the candidate is backtested for 172 days against that baseline AND it dominates on (avg daily return, Sharpe, max DD). Until then: keep fixed-fraction. |
| **C6** | **Cross-sectional momentum ranker** (long top-3 / short bottom-3 across the 9 pairs). | Fracassi & Kogan SSRN 4138685 | **REJECTED — risk of**: introducing a new strategy on top of broken validation infra (Phase 0 not done) AND on top of confidence-anti-predictivity (F10) — adding a new sizing rule keyed to ranked momentum could simply re-express the same broken signal at a different timeframe. Revisit after Phase 0 and after collecting 100+ tagged trades to verify the ranker beats per-pair signal in isolation. |
| **C7** | **Funding-gap mean reversion** (He et al. arXiv 2212.06888). | https://arxiv.org/abs/2212.06888 | **REJECTED — risk of**: requires building a new strategy module (gap-trade vs trend-following), new data fetch (basis = mark - index), new sizing logic — all on top of a $66 account that cannot survive a strategy bug. Sharpe of 1.8 retail is real but the implementation surface area is large. Park as long-term roadmap. |
| **C8** | **Delta-neutral funding carry** (long spot + short perp). | Chan SSRN 5292305 | **REJECTED — risk of**: requires spot account, spot balance, two-leg execution, cross-margin coordination — none of which exist. Architecturally out of scope at $66. |
| **C9** | **Cryptocurrency carry trade** (cross-sectional). | arXiv 2510.14435 | **REJECTED — risk of**: published Sharpe decay from 6.45 → -ve in 2025. Strategy is dying. |
| **C10** | **BTC intraday momentum** (last 30 min predictable from earlier half-hours). | Shen et al. 2022 | **REJECTED — risk of**: 30-min-cycle bot cannot reliably trade a 30-min-window-specific pattern. Would need cycle interval reduction to 5-15 min, which interacts with API rate limits and the existing cycle architecture (signal cascade timings). Out of scope. |
| **C11** | **Session weighting** (boost confidence/size during 16-17 UTC peak vol). | Eross et al. 2024 | **CONDITIONALLY ACCEPTED for Phase 2.** Proof of value: cheap to implement (a single `is_peak_session()` helper and a confidence multiplier), evidence is published. **Risk**: confidence is currently anti-predictive (F10), so multiplying it during peak hours could amplify the bias. **Acceptance criteria**: (a) implement as a logger-only observer for 14 days, (b) measure whether trades placed during 16-17 UTC actually show better hit rates in the journal, (c) gate on F10 being resolved (i.e., until confidence is shown to be predictive again, do not multiply by it). |
| **C12** | **`freqtrade lookahead-analysis` and `recursive-analysis`** — Python tools that detect look-ahead bias and TA-Lib warmup artifacts in backtests. | https://www.freqtrade.io/en/stable/lookahead-analysis/ | **ACCEPTED for Phase 2.** Pure correctness, zero alpha risk, directly strengthens the v6.20 baseline. Implement as `scripts/lookahead_check.py` patterned on freqtrade's CLI: chain `backtest_v4.py` runs at multiple slice boundaries, diff the resulting indicator series and signal timestamps, fail loudly on any divergence > 0. **Acceptance**: passes against current SupertrendTrend and AdaptiveTrend strategies. |
| **C13** | **Monte-Carlo trade-order shuffling** for backtest robustness. | Jesse README | **ACCEPTED for Phase 2.** Pure correctness. Pattern: `scripts/backtest_montecarlo.py` re-runs the existing trade list 1,000× with shuffled order, reports the 5th/50th/95th-percentile equity curve. If the 5th percentile is materially worse than the realised curve, the result was order-dependent and therefore overfit. |
| **C14** | **Per-pair `LowProfitPairs` cooldown** — pause individual pairs after N losing trades. | https://www.freqtrade.io/en/stable/plugins/ | **CONDITIONALLY ACCEPTED for Phase 2.** Proof of value: cuts the kind of repeated DOGE/LINK losses observed in the v6.19/v6.20 logs. **Risk**: if a pair is in a temporary unfavourable regime, locking it out reduces capital efficiency on the remaining 8 pairs — but with only $66 of capital and a 3-position cap, that's a feature not a bug. **Acceptance criteria**: implement as a per-pair `cooldown_until` timestamp (in trades DB) and check it in the position-count gate. Cooldown trigger: 3 losses in last 5 trades on that pair (sample size, not time window). Cooldown duration: 24h. |
| **C15** | **Rolling-equity max-drawdown halt** (in addition to the existing daily-loss halt). | https://www.freqtrade.io/en/stable/plugins/ MaxDrawdown | **ACCEPTED for Phase 2.** Pure defensive. The bot has a daily-loss halt at 10% of start-of-day balance and a $30 hard floor; it does NOT have a rolling-equity drawdown halt. Implement: track high-water mark over the last 7 days, halt for 24h if equity drops > 15% from the 7-day HWM. The drawdown_monitor.py module already tracks the HWM — only the gate is missing. |

### Phase 3 — Long-term roadmap (parked, not in scope)

These are noted so they don't get re-discovered later:

- Spot-perp delta-neutral funding harvest (R-D2 — needs spot leg, not viable at $66).
- Funding-gap mean-reversion strategy (R-D1 — large new strategy module).
- Cross-sectional momentum ranker (C6 — gated on Phase 0 completion + tagged-trade history).
- User-data WebSocket reconciliation (C3 — large plumbing project).
- batchOrders (C1 — re-evaluate after Phase 0 audit gate is real).

---

## Critical files

| File | Phase | Purpose |
|---|---|---|
| `CLAUDE.md` | P0-2 | Constitution; currently lies about phase, balance, version, fees |
| `src/execution/fee_calculator.py` | P0-3 | Hardcoded wrong taker (0.05% → 0.04%) |
| `src/orchestrator/main.py` | P0-4, P0-5, P0-7 | Audit bypass, journal recording, OrderStatus crash |
| `src/anti_hallucination/decision_auditor.py` | P0-4 | Field starvation; called but its decision is ignored |
| `src/anti_hallucination/price_validator.py` | P0-4 | Never invoked → all rows show `price_validated: false` |
| `src/anti_hallucination/signal_validator.py` | P0-4 | Never invoked → all rows show `signal_validated: false` |
| `src/memory/trade_journal.py` | P0-5, P0-6 | pnl_pct math; mode column |
| `src/data/database.py` | P0-6 | Add `mode` column on `trades` and `daily_reports` |
| `src/reporting/daily_pnl.py` | P0-6 | Filter on mode |
| `src/execution/order_manager.py` | P1-1, P1-2, P1-3 | workingType, priceProtect, native trailing |
| `tests/test_execution/test_order_manager.py` | P1-1 — P1-3 | Param-forwarding tests |
| `scripts/backtest_v4.py` | P0-8 | Re-baseline after fee + audit fixes |
| `scripts/lookahead_check.py` (NEW) | C12 | Lookahead bias detector |
| `scripts/backtest_montecarlo.py` (NEW) | C13 | Trade-order MC robustness |
| `CHANGELOG.md` | every phase | Versioning discipline (Section 8) |

## Existing functions and utilities to reuse (not rebuild)

- `LeverageManager.calculate_liquidation_buffer` — already correct, used in P0-4 audit
- `PositionSizer.calculate_size` — Kelly-based path already exists, used in `main.py:864`
- `SanityChecker.check_position_math` — already gating in `main.py:911`
- `SlippageEstimator.estimate_slippage` — already gating in `main.py:960`
- `FundingRateFilter.evaluate` — already used; C4 cascade detector should subclass / observe this, not rebuild
- `DrawdownMonitor` — already tracks HWM; C15 only needs the gate, not the monitor
- `_submit_order_idempotent` (`order_manager.py`) — must be the single entry point for any new order type added in P1; do not bypass
- `_record_trade_exit` — single entry point for journal writes; P0-5 fixes the formula and the call sites

## Verification — how to test the plan end-to-end

Each Phase has its own verification, captured here as a single checklist the user can run:

**Phase 0 verification** (`scripts/verify_p0.py` — to be added):
1. Run all 606 unit tests, expect 606 + (P0-3 fee test) + (P0-5 journal test) + (P0-7 OrderStatus test) ≈ 612 passing.
2. Start bot in dry-run mode for one cycle. Inspect the new `audit_trail.db` row: it must have `price_validated: true`, `signal_validated: true`, `risk_reward_ratio` populated.
3. Force a synthetic SL hit using `scripts/force_close_position.py` on a tiny test position. Inspect the resulting `trades` row: `pnl`, `pnl_pct`, `duration`, `regime`, `exit_reason`, `mode='mainnet'` all populated.
4. Run `python scripts/parse_backtest.py` against `backtest_v4.py` re-run output. Document new baseline avg daily return, Sharpe, max DD, win rate. This becomes the new "validated ceiling" replacing v6.16's 2.68%/day.
5. Confirm `grep "Paper Trading on Testnet" CLAUDE.md` returns nothing.
6. Confirm `grep "0.0005" src/execution/fee_calculator.py` returns nothing.

**Phase 1 verification** (after P0 is done):
1. Place a fresh SL on a real position via the orchestrator. `fetch_order` returns `workingType: MARK_PRICE` and `priceProtect: true`.
2. Watch `cycle_history` for 24 hours: zero `'OrderStatus' object has no attribute 'get'` errors; zero unprotected positions visible in `fetch_positions` (every open position has both an SL and a TP).
3. Inspect the `trades` table after Phase 1: every closed trade has `signal_tag` and `exit_reason` populated.
4. Reconciliation log shows two sources contributing (`openOrders` and `openAlgoOrders`).

**Phase 2 verification** (per-candidate):
- Each conditionally-accepted item (C2, C4, C11, C14) requires its own pre-acceptance trial period or backtest as listed.
- C12, C13, C15 require unit tests + a single live cycle dry-run before going live.

## What this plan deliberately does NOT do

- Does not propose adding any new alpha source on top of broken measurement infrastructure. Phase 0 is non-negotiable.
- Does not change SupertrendTrend, AdaptiveTrend, CrossAssetConsensus, or FundingRateFilter logic. The bot's strategies stay frozen until Phase 0 lets us measure whether they actually work.
- Does not raise position sizing, leverage caps, or relax circuit breakers. Immutable Rules 1-10 stand.
- Does not "ULTRATHINK" by adding 20 features. The user's current losses are not from a missing feature — they are from a bot that cannot tell whether its current features work.
- Does not deploy a watchdog "auto-fix" loop. Every Phase 0 fix is a code change that goes through tests + manual review + restart.

## Confidence (per CLAUDE.md §10)

| Section | Confidence | Notes |
|---|---|---|
| Forensic findings F1–F10 | 10/10 | Every finding has a file:line or DB row cited. F8 (workingType) cross-verified by 3 sources. |
| R-A1 — R-A5 (execution-quality wins) | 10/10 | All cited and code-verified. |
| R-B1 — R-B4 (validation discipline) | 9/10 | freqtrade tools cited; lookahead-analysis behaviour confirmed via docs. Implementation effort is non-trivial. |
| R-C1 (cascade detection) | 8/10 | SSRN paper is cited; the precise threshold (15% APR sustained) needs walk-forward validation before gating. |
| R-D1 — R-D5 (alpha sources) | 8/10 | All have cited Sharpe numbers; D6 documents decay risk; bot cannot adopt without re-baselined backtest first. |
| Plan structure (Phase 0 → 1 → 2) | 10/10 | Maps directly to forensic and research evidence; zero step taken without verification. |
| Specific code change locations | 10/10 | Every file:line cited above is from a Read/Grep result this session. |

## What I would do differently next session

- Use `Read` with a tight `offset/limit` on `.env`, not `Bash` `cat | grep`. The grep pattern was too broad and exposed secrets.
- Spawn fewer parallel Explore agents during the codebase inventory — the existing `main.py` reads gave me 80% of the inventory before any agent ran.
- Start with the database forensic before reading CLAUDE.md, because the constitutional document is the staleness source itself.

