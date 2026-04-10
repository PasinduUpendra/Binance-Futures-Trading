# CLAUDE.md — Claude Quant Autonomous Trading System

> **CONSTITUTIONAL DOCUMENT — 2026-03-15**
> This file is the SUPREME AUTHORITY over all development, trading, and operational decisions.
> Every value in this file MUST match the production codebase and SINGLE_SOURCE_OF_TRUTH.md.
> If a drift is found between this file and code/SSOT, the CODE is truth — update the docs, not the code.
> This project is LIFE OR DEATH. Mistakes are unacceptable. Assumptions are forbidden.

---

## /init — Context Loading Protocol

When starting ANY new session, load context in this exact order:

```
Step 0: Verify you have access to the repo. If not, STOP and ask for files.
Step 1: Read CLAUDE.md (this file) — project constitution
Step 2: Read docs/SINGLE_SOURCE_OF_TRUTH.md — complete reference
Step 3: Read CHANGELOG.md — what changed and when
Step 4: Read .env — current configuration
Step 5: Build a "Source of Truth Map":
        → List immutable rules and compare against code values
        → List active strategies and WHY others are disabled
        → List orchestrator cycle steps
        → FLAG any drift between docs and code immediately
```

**If you skip context loading, your first action MUST be to load it. No exceptions.**

---

## Project Identity

| Field | Value | Source |
|-------|-------|--------|
| **Name** | Claude Quant | — |
| **Purpose** | Autonomous AI trading bot for Binance USDT-M Perpetual Futures | — |
| **Stack** | Python 3.11+ · ccxt async · Claude API · Binance WebSocket | SSOT §13 |
| **Phase** | Paper Trading on Testnet ($5000 balance) | SSOT §1 |
| **Production Balance** | $68.33 USDT (as of 2026-03-13) | SSOT §2, verified via API |
| **Bot PID** | 52685 (v6.17, restarted 2026-04-08) | CHANGELOG 2026-04-08 |
| **Pairs** | BTC, ETH, SOL, DOGE, XRP, LINK, AVAX, SUI, ADA /USDT:USDT | SSOT §6, v6 backtest |

### Performance Reality

| Metric | Value | Source |
|--------|-------|--------|
| **Validated avg daily return** | **2.68%** | v6.16 AGG4 sweep, CHANGELOG 2026-04-07 |
| **Previous validated ceiling** | 1.397% (v6.1, 9 pairs) | v6.1 backtest, CHANGELOG 2026-03-24 |
| **Aspirational target** | **1.0% daily compound** | SSOT §1 — **EXCEEDED (2.68x)** |
| **v6.16 backtest return** | +6,593.5% over 172 days, 197 trades | CHANGELOG 2026-04-07 |
| **v6.16 win rate** | 54.3% | CHANGELOG 2026-04-07 |
| **v6.16 Sharpe** | 7.40 | CHANGELOG 2026-04-07 |
| **v6.16 max drawdown** | 11.4% | CHANGELOG 2026-04-07 |
| **$1,000 milestone** | Day 119 (~4 months from $68.33) | CHANGELOG 2026-04-07 |

> **CRITICAL FRAMING**: v6.16 raised the validated ceiling from 1.397% to 2.68%/day via an 8-configuration parameter sweep (`backtest_aggressive.py`). Key changes: position sizing 15% → 25%, MAX_HOLD_BARS 150 → 100, 9 pairs (was 3). Max drawdown increased from 4.9% to 11.4% — acceptable at $68 balance ($38 margin above $30 floor). This was done under user directive to prepare for real-money deployment. All changes backed by production-code backtest evidence. Any further changes MUST still go through the full strategy versioning pipeline (Section 8).

> **When to exceed 1%**: The bot should capture outsized returns when GENUINE high-confluence setups appear (all filters align, confidence ≥ 85, regime strongly trending, volume surging). This happens through BETTER TRADES, not MORE TRADES. Trail winners with wider stops. Don't close at TP1 on ≥90 confidence signals. Let the market give you the return — don't manufacture it.

### What "Compounding" Means (For Non-Traders)

> Compounding means your gains earn gains. If you make 0.628% on $68, you have $68.43. Tomorrow you earn 0.628% on $68.43 = $0.43. Each day the dollar amount grows slightly. Over 90 days at 0.628%, $68 becomes ~$122. Over 1 year, ~$620. At the aspirational 1%, $68 becomes ~$171 in 90 days, ~$2,568 in a year. Even 0.628% annualized is ~870% — extraordinary by any standard.

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 1: THE TEN IMMUTABLE RULES
# ═══════════════════════════════════════════════════════════════════

These match SSOT §12 exactly. They are HARDCODED in production code. No AI reasoning, no market condition, no "edge case" justifies breaking them.

| # | Rule | Code Location |
|---|------|---------------|
| 1 | **$30 HARD FLOOR** — Balance < $30 = HALT ALL TRADING. No exceptions. | `circuit_breaker.py` DEAD level |
| 2 | **10x MAX LEVERAGE** — Absolute maximum. Circuit breaker reduces this. | `leverage_manager.py` |
| 3 | **3 MAX CONCURRENT POSITIONS** — GREEN=3, YELLOW=2, RED=1. | `circuit_breaker.py` |
| 4 | **25% MAX CAPITAL PER TRADE** — Of total balance (margin, not notional). Raised from 15% via v6.16 backtest evidence. | `position_sizer.py` |
| 5 | **20 MAX DAILY TRADES** — Overtrading prevention. | `risk_params.yaml` |
| 6 | **ALL DATA FROM API** — Never fabricate, estimate, or guess prices. | Anti-hallucination system |
| 7 | **RISK MANAGER APPROVAL** — Every trade must pass risk checks. | `orchestrator/main.py` Step 4 |
| 8 | **CB THRESHOLDS HARDCODED** — Cannot be changed by any agent or code path. | `circuit_breaker.py` |
| 9 | **MIN 2.0 R/R** — Every trade must have at least 2:1 reward/risk ratio. | `supertrend_trend.py` SL=3×ATR, TP=6×ATR |
| 10 | **5% LIQUIDATION BUFFER** — Reject trades where entry-to-liquidation < 5%. | `leverage_manager.py` |

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 2: ANTI-HALLUCINATION & CONFIDENCE PROTOCOL
# ═══════════════════════════════════════════════════════════════════

## Absolute Rules for AI Behavior

```
1. NO ASSUMPTIONS. NO GUESSING.
   If data is missing → STOP and request it or fetch via API.
   If you cannot verify a claim → say "UNVERIFIED" and do NOT act on it.

2. NO GENERALITIES. Every recommendation must be SPECIFIC:
   → Exact file path + function/class + expected behavior + measurable impact.

3. NO HALLUCINATIONS. Ground ALL claims in cited sources:
   (A) Repo files with exact paths and line numbers
   (B) Binance official documentation (developers.binance.com)
   (C) Verified backtest results from this project's own test suite

4. CONFIDENCE GATE: If confidence < 10/10, do NOT implement. Instead produce:
   (1) What you KNOW (with citations)
   (2) What you MUST VERIFY
   (3) The minimal experiment needed to reach 10/10

5. FORBIDDEN PHRASES in trade analysis:
   NEVER: "the price will," "guaranteed," "certain to," "definitely," "impossible"
   ALWAYS: "suggests," "indicates," "probability of," "evidence supports"
```

## Five-Layer Defense System (Matches SSOT §7)

| Layer | Module | What It Checks |
|-------|--------|---------------|
| 1. Data Validation | `data_validator.py` | OHLCV integrity, timestamp freshness (<2 candles), low≤high |
| 2. Price Validation | `price_validator.py` | Cross-reference 2 sources within 0.1%, within daily range |
| 3. Signal Validation | `signal_validator.py` | Signal references specific indicator values, passes strategy math |
| 4. Decision Audit | `decision_auditor.py` | Every trade includes reasons NOT to trade, raw data timestamps |
| 5. Execution Verification | `order_manager.py` | Separate GET after POST, fill price within 0.5% expected |

> ⚠️ **TEST COVERAGE WARNING**: Layers 2, 3, and 4 (`price_validator.py`, `signal_validator.py`, `decision_auditor.py`) currently lack dedicated unit tests. See Section 11.

## Proof of Correctness Protocol

For EVERY trade decision AND every code change, provide:

| Proof Type | What It Shows | Required For |
|-----------|---------------|-------------|
| **Source Proof** | Which official docs / repo functions define the behavior | ALL changes |
| **Test Proof** | Which tests cover it (or write tests FIRST) | ALL changes |
| **Backtest Proof** | Baseline vs change using `backtest_v4.py` production code paths | Strategy changes |
| **Risk Proof** | Max loss scenario, liquidation distance, fee+funding worst-case | Trade decisions |

If ANY proof is missing → PAUSE. Do not proceed.

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 3: BINANCE FUTURES MECHANICS
# ═══════════════════════════════════════════════════════════════════

> **For the non-trader**: Binance Futures lets you bet on crypto price movements using borrowed money (leverage). "Perpetual" means the contracts never expire. You don't own the crypto — you own a contract tied to its price.

## Account Configuration (Verified 2026-03-13 via API — SSOT §2)

```python
EXCHANGE_CONFIG = {
    'class': 'binanceusdm',              # USDT-M Futures ONLY
    'enableRateLimit': True,              # ALWAYS True
    'options': {'adjustForTimeDifference': True, 'defaultType': 'future'}
}

# Symbol format: ALWAYS 'BASE/QUOTE:SETTLE'
# ✅ 'BTC/USDT:USDT', 'ETH/USDT:USDT'
# ❌ 'BTCUSDT', 'BTC/USDT'

# Position mode: ONE-WAY (not Hedge) — verified on account
# Margin mode: ISOLATED (Multi-Assets Mode = FALSE) — verified on account
# TESTNET vs MAINNET: Separate API keys, NEVER mixed.
```

## Binance Testnet Operational Details

> **For custom tooling, watchdogs, and reconciliation** — ccxt abstracts these via `enable_demo_trading(True)`, but any direct HTTP/WebSocket client must use the correct endpoints.

| Resource | Testnet | Production |
|----------|---------|------------|
| **REST base URL** | `https://demo-fapi.binance.com` | `https://fapi.binance.com` |
| **WebSocket base** | `wss://fstream.binancefuture.com` | `wss://fstream.binance.com` |
| **WebSocket stream** | `wss://fstream.binancefuture.com/ws` | `wss://fstream.binance.com/ws` |

### User Data Stream Constraints (Applies to BOTH testnet and production)

| Constraint | Value | Impact |
|-----------|-------|--------|
| **Listen key expiry** | 60 minutes | Must call `PUT /fapi/v1/listenKey` before expiry to keep alive |
| **Connection validity** | 24 hours max | Single user-data WS connection dies after 24h — must reconnect |
| **Keep-alive interval** | ≤ 30 minutes recommended | Send keepalive well before the 60-min expiry |

### How ccxt Handles This

The codebase uses `ccxt_async.binanceusdm` with `enable_demo_trading(True)` for testnet (NOT the deprecated `set_sandbox_mode(True)` which routes to dead endpoints — see `market_data.py:161`). ccxt internally:
- Routes REST calls to `demo-fapi.binance.com`
- Routes WS to `fstream.binancefuture.com`
- Manages listen key renewal automatically when using its WS implementation

### When This Matters

If you build tooling that bypasses ccxt (custom watchdog WebSocket, direct REST reconciliation, listen-key management), you MUST:
1. Use the correct base URLs from the table above
2. Implement listen key keepalive (PUT every 30 min)
3. Handle 24h reconnection gracefully
4. Use separate API keys — testnet keys do NOT work on production and vice versa (ERR-20260313-001)

## Fee Structure (Verified via API — SSOT §2, §5.6)

| Scenario | Fee | Calculation | Source |
|----------|-----|-------------|--------|
| Maker (limit order) | **0.02%** | 0.0002 × notional | API verified |
| Taker (market order) | **0.05%** | 0.0005 × notional | API verified |
| Round-trip (maker entry + taker exit) | **0.07%** | 0.0002 + 0.0005 | SSOT §2 |
| Round-trip (taker both sides) | **0.10%** | 0.0005 + 0.0005 | SSOT §5.6 |
| Round-trip (taker both + BNB discount) | **0.09%** | 0.10% × 0.9 | SSOT §5.6 |
| BNB Burn discount | **10% off** | ENABLED on account | API verified |

**Use Post-Only (GTX) limit orders for entries when possible → pays 0.02% instead of 0.05%.**
**TP targets are adjusted to be net-of-fees profitable in `fee_calculator.py`.**

> **For the non-trader**: Every trade has a cost (fee). If you buy and sell using market orders, it costs about 0.10% of your trade size. Your trade must profit more than 0.10% just to break even.

## Funding Rate

> **For the non-trader**: Every 8 hours, one side of the market pays the other. If "funding rate" is positive, longs pay shorts. High positive funding often means too many people are betting on price going up — a correction may follow.

- Funding interval: **Every 8h** (00:00, 08:00, 16:00 UTC)
- Current rates (SSOT §2): ETH 0.0029%, SOL 0.0045%, DOGE 0.01%
- Factor funding into hold-time decisions: 0.01% per 8h = ~0.03%/day = ~1%/month drag

## Mark Price vs Last Price

> **For the non-trader**: "Last price" is the most recent trade. "Mark price" is Binance's calculated "fair price" that smooths out manipulation. Binance uses MARK PRICE to decide when to liquidate you. Always use mark price for risk calculations.

## Liquidation Modeling

> **For the non-trader**: If your trade loses enough, Binance automatically closes it ("liquidates") to prevent losses exceeding your margin. Higher leverage = closer liquidation price = more danger.

**The heuristic** `1 / leverage` gives approximate liquidation distance:

| Leverage | Approx Distance | Notes |
|----------|-----------------|-------|
| 3x | ~33% | Safe ✅ |
| 5x | ~20% | Recommended default ✅ |
| 7x | ~14% | BTC/ETH only ⚠️ |
| 10x | ~10% | BTC only, ≥85 confidence ⚠️ |

> ⚠️ **THIS IS AN APPROXIMATION ONLY.** Actual Binance liquidation depends on: mark price (not last price), maintenance margin rate (varies by position tier), accumulated funding payments, and unrealized PnL. The heuristic is useful for quick mental math but is NOT the final safety check.

**The authoritative check** must call `exchange.fetch_positions()` to get Binance's calculated liquidation price, then verify the buffer against that real number. The code in `leverage_manager.py` uses the heuristic for pre-trade estimation (SSOT §5.4), and `order_manager.py` should verify against exchange-reported liquidation price post-fill.

**Rule**: Stop-loss must be placed at LESS than 50% of the ACTUAL (exchange-reported) liquidation distance. The 5% minimum buffer (Immutable Rule #10) applies to the exchange-reported value.

## Idempotent Order Submission (IMPLEMENTED — `order_manager.py`)

**The problem**: When you submit an order and get a timeout or HTTP 503, you don't know if the order was placed or not. Binance explicitly states that 503 does NOT mean the order failed — it may have succeeded. Retrying without deduplication can place DUPLICATE orders, doubling your leveraged exposure instantly.

**Implementation** (added 2026-03-15 — CHANGELOG, SSOT §15 item 13 RESOLVED):

Three methods in `src/execution/order_manager.py`:

1. **`_submit_order_idempotent()`** — Core idempotent logic:
   - Generates unique `newClientOrderId` (`cq_<uuid16>`) per attempt
   - On `NetworkError`/timeout → calls `_query_by_client_order_id()` → returns existing if found, retries with NEW ID if not
   - On `InsufficientFunds`/`InvalidOrder` → returns `None` (no retry)
   - On `DDoSProtection` → safe to retry (order definitely not placed)
   - Max 3 attempts with exponential backoff (2s, 4s)

2. **`_query_by_client_order_id(symbol, client_oid)`** — Queries Binance via `fapiPrivateGetOrder` with `origClientOrderId` parameter. 2-second propagation wait before query.

3. **`_order_result_from_status(status, client_oid)`** — Converts query result to `OrderResult`.

All 4 order methods (`place_market_order`, `place_limit_order`, `place_stop_loss`, `place_take_profit`) are thin wrappers that delegate to `_submit_order_idempotent()`. The unsafe `@_retry` decorator was removed from all order methods.

**Return type**: `OrderResult | None` (None = graceful failure, no order placed).

> ⚠️ **TEST GAP**: These methods are P0 priority for unit tests (15+ needed). See Section 11.

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 4: CIRCUIT BREAKERS & RISK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

## Circuit Breaker Levels (HARDCODED — `src/risk/circuit_breaker.py` — SSOT §5.1)

| Level | Balance | Max Leverage | Max Positions | Size Multiplier | Trading |
|-------|---------|-------------|---------------|-----------------|---------|
| GREEN | ≥ $60 | 10x | 3 | 1.0x | YES |
| YELLOW | ≥ $45 | 5x | 2 | 0.5x | YES |
| RED | ≥ $30 | 3x | 1 | 0.25x | CONDITIONAL |
| DEAD | < $30 | 0 | 0 | 0 | **HALT** |

## Temporal Circuit Breakers (SSOT §5.1)

| Trigger | Action | Source |
|---------|--------|--------|
| **Daily loss > 10%** of start-of-day balance | HALT until next UTC day | SSOT §5.1 line 322, orchestrator Step 1 |
| **5 consecutive losses** | 2-hour pause | SSOT §5.1 line 323 |
| **RED level** | Requires ≥ 2/3 win rate on last 10 trades to trade | SSOT §5.1 line 324 |

> **NOTE ON DAILY LOSS ALERTS**: The daily P&L reporter (wired into orchestrator at midnight UTC) alerts on daily loss > 5% (CHANGELOG 2026-03-14). This is an ALERT threshold for human awareness, NOT a trading halt. The actual halt threshold is 10% in the circuit breaker code.

## Position Sizing — Confidence-Based (v6.16 — SSOT §5.2)

Raised from 15% to 25% max sizing based on 8-config backtest sweep (2026-04-07). Code in `orchestrator/main.py`.

```
if confidence >= 60%:  position_pct = 25%    of balance (margin)
elif confidence >= 45%: position_pct = 16.7%
else:                   position_pct = 11.7%

position_pct *= CB_size_multiplier    # GREEN=1.0, YELLOW=0.5, RED=0.25
margin = balance × position_pct
margin = max(margin, $5)              # Binance minimum notional
margin = min(margin, balance × 25%)   # Hard cap (Rule #4)
notional = margin × leverage
# Per-pair minimum notional check: BTC=$100, ETH=$20, others=$5
```

**Note**: Half-Kelly code in `kelly_criterion.py` is retained but NOT used in the orchestrator.

## Dynamic Leverage (SSOT §5.3 — `leverage_manager.py`)

| Confidence | Regime | Leverage | Midpoint |
|-----------|--------|----------|----------|
| 80-100% | TRENDING | 7-10x | 8x |
| 60-79% | TRENDING | 5-7x | 6x |
| 60-79% | VOLATILE/RANGING | 3-5x | 4x |
| 40-59% | TRENDING | 3-5x | 4x |
| 40-59% | VOLATILE/RANGING | 2-3x | 2x |
| 25-39% | TRENDING | 2-3x | 2x |
| 25-39% | VOLATILE | 1-2x | 1x |
| 25-39% | RANGING | 2-3x | 2x |
| < 25% | Any | 0 | NO TRADE |
| Any | QUIET | 0 | NO TRADE |

CB caps override: YELLOW max 5x, RED max 3x, DEAD 0x.
GARCH volatility model adjusts leverage downward during vol spikes (Step 4 of orchestrator).

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 5: STRATEGY SYSTEM
# ═══════════════════════════════════════════════════════════════════

## Active Strategy: SupertrendTrend ONLY (SSOT §4.3)

| Parameter | Value | Source |
|-----------|-------|--------|
| Regime required | TRENDING (ADX ≥ 18) | `adaptive_strategy.py` |
| Timeframe | 4H indicators, 1H close for entry price | SSOT §4.1 |
| **Supertrend period** | **8** (was 10) | `indicator_engine.py`, v5 sweep 2026-03-16 |
| **Supertrend multiplier** | **2.0** (was 3.0) | `indicator_engine.py`, v5 sweep 2026-03-16 |
| Entry LONG | 4H Supertrend flips bearish→bullish AND ADX ≥ 18 | `supertrend_trend.py` |
| Entry SHORT | 4H Supertrend flips bullish→bearish AND ADX ≥ 18 | `supertrend_trend.py` |
| Stop-loss | 3.0× ATR(4H) from entry | SSOT §4.3 |
| Take-profit | 6.0× ATR(4H) from entry (R/R = 2.0) | SSOT §4.3 |
| Trailing stop | Activate at 2.0× ATR(4H), trail at 2.5× ATR(4H) | SSOT §4.3 |
| **Reversal exit** | **Tighten SL to breakeven** (was: close immediately) | v5 sweep 2026-03-16 |
| **Max hold time** | **100 bars (~4.17 days)** — force close after | `orchestrator/main.py`, v6.16 sweep |
| Confidence | Base flip 40pts + ADX 20pts + EMA alignment 20pts + RSI 10pts + flip quality 10pts | SSOT §4.3 |

## Disabled Strategies (With Evidence — SSOT §4.3, CHANGELOG 2026-03-15)

| Strategy | Win Rate | P&L | Reason | Code Location |
|----------|----------|-----|--------|---------------|
| MeanReversion | 5.3% | -$7.65 | 2-of-3 confirmation too loose | `mean_reversion.py` — retained |
| BreakoutTrader | 23.9% | -$1.13 | Negative EV | `breakout_trader.py` — retained |
| TrendFollower | 30.0% | +$0.35 | Marginal, not worth risk | `trend_follower.py` — retained |

## Regime Detection (SSOT §4.2 — `regime_detector.py`)

| Regime | ADX | Action |
|--------|-----|--------|
| TRENDING (ADX ≥ 18) | > 20 | SupertrendTrend (4H) — only active route |
| TRENDING (ADX < 18) | > 20 | NO TRADE (TrendFollower disabled) |
| RANGING | < 20 | NO TRADE (MeanReversion disabled) |
| VOLATILE | 15-30 | NO TRADE (BreakoutTrader disabled) |
| QUIET | < 15 | NO TRADE |

## Futures Sentiment Data (Proxy Feeds — NOT Official Smart Signal)

> ⚠️ **IMPORTANT LABELING**: The endpoints below are standard Binance Futures aggregate sentiment data. They are NOT the same as the Binance "Smart Money" / "Smart Signal" product (binance.com/en/smart-money), which provides richer data including dominant flow direction per trader, individual trader counts and notional sizes, average entry price per side, unrealized PnL aggregation, and profitable-trader share. If direct Smart Signal API access becomes available, a separate integration layer should be built. Do not conflate these proxy feeds with official Smart Signal parity.

**Available proxy endpoints:**
- `/fapi/v1/topLongShortAccountRatio` — Top trader long/short by account count
- `/fapi/v1/topLongShortPositionRatio` — Top trader long/short by position size
- `/fapi/v1/takerlongshortRatio` — Taker buy/sell volume ratio
- `/fapi/v1/openInterest` — Current open interest

**Usage as supplementary signals** (not primary — must be validated through strategy pipeline):
- Top trader long > 65% AND funding > 0.05% → crowded long, reduce long confidence
- Taker buy/sell > 1.3 → aggressive buying, momentum continuation possible
- OI rising + price rising → new money entering, trend may continue
- OI falling + price falling → forced liquidations, possible capitulation

**Any strategy derived from this data MUST pass the full versioning pipeline (Section 8).**

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 6: ORCHESTRATOR — 7-STEP CYCLE (SSOT §6)
# ═══════════════════════════════════════════════════════════════════

**Cycle interval**: 30 minutes (1800 seconds) — `src/orchestrator/main.py` (v6.17: reduced from 1h)

```
Step 1: SENTINEL (Circuit Breaker)
  ├── Fetch balance from Binance API
  ├── Update drawdown monitor (high-water mark)
  ├── Check CB level (GREEN/YELLOW/RED/DEAD)
  ├── Check daily loss (>10% of start-of-day = HALT until next UTC day)
  ├── Check consecutive losses (5+ = 2h pause)
  └── If not allowed → skip cycle

Step 1b: FETCH MULTI-TIMEFRAME DATA
  ├── For each pair (ETH, SOL, DOGE):
  │   ├── Fetch 4H OHLCV (200 candles) + calculate all indicators
  │   ├── Fetch 1H OHLCV (200 candles) + calculate all indicators
  │   └── Validate data (anti-hallucination Layer 1)
  └── Store as dict[symbol] → (df_4h, df_1h)

Step 2: SUPERTREND REVERSAL EXITS (TIGHTEN TO BREAKEVEN)
  ├── For each open position:
  │   ├── Check if 4H Supertrend flipped against direction
  │   ├── If flipped: cancel existing SL/TP → place new SL at entry price (breakeven)
  │   └── Position stays open — trailing stop continues tracking
  └── v5 sweep: tighten_to_breakeven beats immediate close on all metrics

Step 2b: TRAILING STOP MANAGEMENT
  ├── For each open position with TrailingStopState:
  │   ├── Update best_price (track favorable movement)
  │   ├── If moved 2.0× ATR(4H) favorably → activate trailing stop
  │   ├── If activated + pullback 2.5× ATR(4H) from best → close position
  │   └── Log state changes

Step 2c: TIME-BASED EXITS (MAX_HOLD_BARS = 100)
  ├── For each open position:
  │   ├── Calculate hours held = (now - entry_time) / 3600
  │   ├── If hours_held ≥ 100: close at market, cancel orders, clean trailing stop
  │   └── Record as "time_exit" reason
  └── Prevents capital lock-up in stale positions (~4.17 day cap, v6.16)

Step 3: MULTI-TIMEFRAME SIGNAL GENERATION (v6.17: multi-signal)
  ├── For each pair:
  │   ├── AdaptiveStrategy.get_signal_multi_tf(df_4h, df_1h)
  │   ├── Regime detection on 4H data
  │   ├── Route: SupertrendTrend gets 4H (only active route)
  │   └── Keep signals with confidence ≥ 25%
  ├── Collect ALL valid signals (not just best)
  └── Sort by confidence, execute ALL up to position limit

Step 4: RISK MANAGEMENT
  ├── Check position count vs CB max
  ├── Determine leverage (confidence × regime × CB)
  ├── GARCH volatility adjustment (reduce leverage during vol spikes)
  ├── Calculate position size (confidence-based: 7/10/15% × CB multiplier)
  ├── Verify liquidation buffer ≥ 5% (against exchange-reported liquidation price)
  └── Sanity check all math

Step 5: DECISION AUDIT
  └── Log devil's advocate counter-arguments

Step 6: EXECUTION
  ├── Set leverage on exchange
  ├── Calculate order quantity (notional / entry_price)
  ├── Place market order (with newClientOrderId for idempotency — see Section 3)
  ├── Verify fill (separate GET call)
  ├── Place stop-loss (STOP_MARKET, reduceOnly=True)
  ├── Place take-profit (TAKE_PROFIT_MARKET, reduceOnly=True) — added v4
  ├── Initialize TrailingStopState for new position
  └── Log trade details

Step 7: MEMORY
  └── Record trade to journal (trade_journal.py + database.py)
```

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 7: WATCHDOG / SENTINEL SYSTEM
# ═══════════════════════════════════════════════════════════════════

## Architecture

The watchdog is a **Claude agent** (`.claude/agents/watchdog.md`), NOT a simple Python script. Invoked via `@watchdog` in Claude Code sessions. Uses `scripts/watchdog_tools.py` for structured data extraction (5 subcommands: health, logs, performance, mistakes, market).

## Agent Count Reconciliation

The file tree in SSOT §3 (line 68) lists **8 agent definitions** in `.claude/agents/`:
orchestrator, sentinel, market-analyst, strategy-selector, risk-manager, execution-agent, memory-agent, daily-reporter.

The **watchdog** (`watchdog.md`) was added on 2026-03-15 (CHANGELOG), bringing the total to **9 agents**. The SSOT file tree header still says "8 AI agent definitions" — this is stale and should be updated to 9.

## Watchdog Capabilities

| Check | What It Detects |
|-------|-----------------|
| `health` | Bot crashes, process status, cycle freshness |
| `logs` | Errors, slow cycles, no-signal periods |
| `performance` | Win rate drift, daily P&L vs target gap, strategy degradation |
| `mistakes` | Missing TP/SL orders, incorrect order types, wrong reduceOnly |
| `market` | Live regime state, supertrend direction/flip status per pair |

## Safe Action Set (Autonomous — No Human Approval)

| Action | Trigger |
|--------|---------|
| Log incident with unique ID | Any anomaly |
| Alert via reporting | Performance drift detected |
| Suggest specific fixes | Detected issues |

## Human Approval Required

| Action | Trigger |
|--------|---------|
| Deploying new strategy to live | Any strategy change |
| Changing risk parameters | Any risk param modification |
| Increasing leverage limits | Any leverage cap change |
| Resuming after DEAD | Balance recovery above $30 |

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 8: STRATEGY VERSIONING PIPELINE
# ═══════════════════════════════════════════════════════════════════

**No strategy goes live without passing this pipeline. No exceptions.**

```
Unit Tests → Backtest (production code via backtest_v4.py) → Walk-Forward OOS → Paper Trading → Live
     ↓              ↓                                           ↓                    ↓              ↓
   100% pass    PF > 1.5                                    OOS PF > 1.2        Matches backtest  Monitor
               WR > 55%                                    No degradation       within ±20%       Regression
               Sharpe > 1.5                                                                       → rollback
               Max DD < 15%
```

**Every strategy change MUST:**
1. Increment a version number
2. Write a CHANGELOG entry linking to evidence
3. Be defined in machine-readable spec (YAML in `config/strategies/`)
4. Have rollback capability (one command)
5. Auto-rollback when monitoring detects regression (win rate drops below 50% over 20 trades)

**CRITICAL**: The backtest MUST use production code paths (actual classes: AdaptiveStrategy, PositionSizer, LeverageManager). The v3→v4 divergence discovery (CHANGELOG 2026-03-15) proved that inline backtests with different logic produce unreliable results. Always backtest with `backtest_v4.py` pattern.

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 9: DOCUMENT-DRIVEN DEVELOPMENT
# ═══════════════════════════════════════════════════════════════════

## Required Documents

| Document | Purpose | Location |
|----------|---------|----------|
| **CLAUDE.md** | This file — project constitution | Root |
| **SINGLE_SOURCE_OF_TRUTH.md** | Complete reference (file tree, functions, params) | `docs/` |
| **CHANGELOG.md** | What changed and when | Root |
| **DESIGN.md** | Architecture, invariants, interfaces | `docs/plans/` |
| **SAFETY.md** | Circuit breakers, kill switches, liquidation protection | `docs/` |
| **STRATEGY.md** | Each strategy as spec (inputs, signals, exits, failure modes) | `docs/` |
| **EVIDENCE.md** | How changes are proven (tests, backtests, paper trading) | `docs/` |

## Document-First Workflow

```
1. DESIGN (write spec) → 2. TEST (write tests first) → 3. IMPLEMENT → 4. VALIDATE → 5. DEPLOY
```

## Drift Prevention

When updating ANY document:
- Cross-check values against ALL other docs and code
- If a value appears in multiple places, update ALL occurrences
- If code and docs disagree, CODE IS TRUTH — update docs to match
- Flag any discovered drift as an incident in `.learnings/ERRORS.md`

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 10: STRUCTURED OUTPUT FORMAT
# ═══════════════════════════════════════════════════════════════════

Every response from Claude working on this project MUST follow this structure:

```
## A) Confidence Score (0–10)
Must be 10/10 to implement. Otherwise research/spec only.

## B) What Was Verified
Specific files, functions, line numbers, data points checked.

## C) What Is Unknown / Needs Clarification
Explicit questions. If anything unknown, confidence CANNOT be 10/10.

## D) Findings (Ranked by Severity)
Each with: exact location (file:line), impact, suggested fix.

## E) Proposed Changes
For each: exact files, functions/classes, tests to add, acceptance criteria, proof of correctness.

## F) Implementation (Only at 10/10)
Minimal patch plan with rollback procedure.
```

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 11: TEST COVERAGE STATUS & PRIORITY
# ═══════════════════════════════════════════════════════════════════

**Current**: 598 tests passing (~1.75s) — SSOT §14

### Covered Modules (with test counts)

| Module | Tests | Key Coverage |
|--------|-------|-------------|
| circuit_breaker.py | 23 | All CB levels, daily loss halt, streaks |
| leverage_manager.py | 23 | Confidence tiers, CB caps, liquidation buffer |
| trade_journal.py | 20 | CRUD, win rate, consecutive losses |
| position_sizer.py | 18 | Kelly sizing, CB multipliers, caps |
| daily_pnl.py | 18 | Daily calc, Sharpe, doubling progress |
| drawdown_monitor.py | 17 | High-water mark, persistence, reset |
| supertrend_trend.py | 15 | Flip detection, ADX gate, SL/TP math |
| volatility_model.py | 15 | GARCH, EWMA fallback |
| trailing_stop.py | 13 | Activation, trail trigger, best price |
| sanity_checks.py | 11 | Price/signal validation |
| adaptive_multi_tf.py | 10 | Regime routing, ST reversal |
| kelly_criterion.py | 8 | Sizing, edge cases |
| strategies.py | 8 | Individual signal generation |
| pipeline.py | 7 | End-to-end orchestrator |
| regime_detector.py | 5 | Classification |
| fee_calculator.py | 4 | Maker/taker, BNB |

### UNTESTED Modules — Priority by Blast Radius (SSOT §14 Test Coverage Gaps)

| Priority | Module | Why It Matters | Min Tests Needed |
|----------|--------|---------------|-----------------|
| **P0 — DONE** | `order_manager.py` | 33 tests (idempotent submission, parsing, cancel/query, leverage) | ✅ Covered |
| **P0 — DONE** | `price_validator.py` | 13 tests (24h range, deviation, staleness, cross-validation) | ✅ Covered |
| **P0 — DONE** | `signal_validator.py` | 13 tests (indicator specificity, value matching, R/R, entry price) | ✅ Covered |
| **P1 — HIGH** | `database.py` | Stores trade journal driving sizing decisions. | 10+ (CRUD, migration, WAL mode, edge cases) |
| **P1 — HIGH** | `decision_auditor.py` | Anti-hallucination Layer 4. Audit trail integrity. | 6+ |
| **P1 — HIGH** | `market_data.py` | All data flows through this. | 8+ (fetch, validation, error handling) |
| **P1 — HIGH** | `position_tracker.py` | Tracks open positions for risk checks. | 6+ |
| **P2 — MEDIUM** | `correlation_monitor.py` | Multi-position risk. | 5+ |
| **P2 — MEDIUM** | `data_validator.py` | Anti-hallucination Layer 1. | 5+ |
| **P2 — MEDIUM** | `slippage_estimator.py` | Execution quality. | 4+ |
| **P2 — MEDIUM** | `indicator_engine.py` | TA calculations. | 6+ |
| **P3 — LOW** | `performance_tracker.py`, `bias_detector.py`, `trade_memory_client.py` | Memory system. | 4+ each |
| **P3 — LOW** | `dashboard.py`, `alert_system.py`, `report_generator.py`, `candle_store.py` | Reporting/caching. | 3+ each |
| **P3 — LOW** | `main.py` (orchestrator) | Only integration coverage via test_pipeline.py. | Complex — integration tests sufficient for now |

**Rule**: No new feature work until P0 modules have dedicated tests. This is not optional.

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 12: EMERGENCY PROCEDURES
# ═══════════════════════════════════════════════════════════════════

## Kill Switch (Three Independent Methods)

1. `python scripts/kill_switch.py` — Command line
2. `KILL_SWITCH=true` in env → immediate halt
3. Watchdog auto-trigger on CRITICAL events

### Procedure
```
1. Cancel ALL pending orders on ALL symbols
2. Close ALL open positions at MARKET
3. Set bot state to HALTED
4. Send alert
5. Log complete system state snapshot
6. Do NOT resume until human explicitly re-enables
```

## Position Reconciliation (Every 5 Minutes)

```
1. Fetch all open positions from Binance (exchange.fetch_positions())
2. Compare with local position tracker
3. Mismatch → use BINANCE STATE as truth, update local
4. Phantom positions (on Binance but not local) → close immediately
5. Missing SL/TP orders → place immediately
6. Log all reconciliation events as WARNING
```

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 13: CODE STANDARDS & KEY PATHS
# ═══════════════════════════════════════════════════════════════════

## Standards

- Python 3.11+, type hints on ALL functions
- Pydantic models for ALL data structures (`frozen=True`)
- `Decimal` for ALL monetary values
- Async/await for exchange I/O
- UTC timestamps everywhere
- Structured logging with context (agent name, trade ID)
- Every module has `__init__.py` with public API exports

## Key Paths

| Path | Purpose | Safety Level |
|------|---------|-------------|
| `CLAUDE.md` | This file | CONSTITUTIONAL |
| `docs/SINGLE_SOURCE_OF_TRUTH.md` | Complete reference | CONSTITUTIONAL |
| `CHANGELOG.md` | Change history | REQUIRED |
| `src/orchestrator/main.py` | Main 7-step loop | CRITICAL |
| `src/risk/circuit_breaker.py` | CB levels | SAFETY CRITICAL |
| `src/strategies/supertrend_trend.py` | Active strategy | CRITICAL |
| `src/strategies/adaptive_strategy.py` | Regime router | CRITICAL |
| `src/execution/order_manager.py` | Order execution | SAFETY CRITICAL |
| `src/risk/leverage_manager.py` | Leverage + liq buffer | CRITICAL |
| `src/risk/position_sizer.py` | Position sizing | CRITICAL |
| `scripts/backtest_v4.py` | Production-code backtest | VALIDATION |
| `.claude/agents/watchdog.md` | Watchdog agent (9th agent) | MONITORING |
| `tests/` | 598 tests | QUALITY |
| `.learnings/` | Self-improving knowledge base | LEARNING |
| `config/` | risk_params.yaml, regime_params.yaml, circuit_breakers.yaml | CONFIG |

## Self-Improving Learnings System

When encountering corrections, API quirks, failed assumptions, or bugs:
1. Log in `.learnings/LEARNINGS.md` (format: `LRN-YYYYMMDD-NNN`)
2. Log errors in `.learnings/ERRORS.md` (format: `ERR-YYYYMMDD-NNN`)
3. When a pattern recurs 3+ times → promote to permanent rule in this file
4. Before logging → check if similar entry exists → link with "See Also" and bump priority

---

# ═══════════════════════════════════════════════════════════════════
# SECTION 14: SCALING ROADMAP
# ═══════════════════════════════════════════════════════════════════

| Phase | Account Size | Unlock Criteria |
|-------|-------------|----------------|
| **Paper** (CURRENT) | $5000 simulated | 200+ trades, 2+ weeks, WR > 55% |
| **Micro-Live** | $68-$150 | SupertrendTrend only, BTC+ETH only |
| **Growth** | $150-$500 | Add momentum strategy IF pipeline-validated |
| **Diversified** | $500-$2K | Add mean reversion, add SOL, 3 concurrent |
| **Advanced** | $2K+ | Full multi-strategy, SMC layer, 5 concurrent |

---

**Last Updated**: 2026-04-08
**Version**: 3.1.0
**Reconciled Against**: SINGLE_SOURCE_OF_TRUTH.md (2026-03-15), CHANGELOG.md (2026-03-15)
**Fixes Applied**: Daily loss threshold (10% not 3%), fee scenario clarification, agent count (9), cycle steps (7), idempotent order submission, liquidation heuristic caveat, Smart Money labeling, test coverage gaps, performance framing
**Next Review**: After paper trading completes (~2026-03-21) or any CRITICAL incident
