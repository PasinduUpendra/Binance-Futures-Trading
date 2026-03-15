---
name: binance-futures-trading
description: >
  Binance USDT-M Perpetual Futures trading expertise. Use when: placing orders, managing positions,
  checking funding rates, handling liquidation, WebSocket streams, testnet vs mainnet, order types
  (STOP_MARKET, TAKE_PROFIT_MARKET, LIMIT), margin modes, leverage, listen key management,
  idempotent order submission, or any Binance Futures API interaction.
applyTo: "src/execution/**,src/orchestrator/**,src/data/market_data.py,scripts/watchdog_tools.py,config/**"
---

# Binance USDT-M Perpetual Futures Trading — Comprehensive Skill

## Scope

This skill covers ALL Binance USDT-M perpetual futures operations for the Claude Quant trading system. It is the authoritative reference for exchange interaction patterns, order management, position lifecycle, and testnet/mainnet configuration.

---

## 1. Account Configuration (Verified via API)

```python
EXCHANGE_CONFIG = {
    'class': 'binanceusdm',              # USDT-M Futures ONLY
    'enableRateLimit': True,              # ALWAYS True — prevents 429 bans
    'options': {
        'adjustForTimeDifference': True,
        'defaultType': 'future',
    }
}

# Symbol format: ALWAYS 'BASE/QUOTE:SETTLE'
# ✅ 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'DOGE/USDT:USDT'
# ❌ 'ETHUSDT', 'ETH/USDT'  — will cause ccxt errors

# Position mode: ONE-WAY (not Hedge) — verified on account
# Margin mode: ISOLATED (Multi-Assets Mode = FALSE)
# TESTNET vs MAINNET: Separate API keys, NEVER mixed
```

### Testnet vs Production URLs

| Resource | Testnet | Production |
|----------|---------|------------|
| REST base | `https://demo-fapi.binance.com` | `https://fapi.binance.com` |
| WebSocket base | `wss://fstream.binancefuture.com` | `wss://fstream.binance.com` |
| WebSocket stream | `wss://fstream.binancefuture.com/ws` | `wss://fstream.binance.com/ws` |

**ccxt handles routing** via `exchange.enable_demo_trading(True)` — do NOT use deprecated `set_sandbox_mode(True)`.

---

## 2. Order Types & Execution Patterns

### Supported Order Types

| Type | Binance Type | Use Case | Code Location |
|------|-------------|----------|---------------|
| Market | `MARKET` | Entry orders (taker) | `order_manager.py:place_market_order()` |
| Limit | `LIMIT` | Entry orders (maker, preferred) | `order_manager.py:place_limit_order()` |
| Stop-Loss | `STOP_MARKET` | Protective SL, `reduceOnly=True` | `order_manager.py:place_stop_loss()` |
| Take-Profit | `TAKE_PROFIT_MARKET` | TP exit, `reduceOnly=True` | `order_manager.py:place_take_profit()` |
| Post-Only | `LIMIT` + `GTX` | Guaranteed maker fee (0.02%) | `order_manager.py` |

### Idempotent Order Submission (CRITICAL)

All orders use `newClientOrderId` (`cq_<uuid16>`) for deduplication:

```python
# Pattern from order_manager.py:_submit_order_idempotent()
1. Generate unique client_order_id = f"cq_{uuid4().hex[:16]}"
2. Submit order with newClientOrderId
3. On NetworkError/timeout:
   a. Wait 2s propagation delay
   b. Query _query_by_client_order_id(symbol, client_oid)
   c. If found → return existing order (NO duplicate)
   d. If not found → retry with NEW client_order_id (max 3 attempts)
4. On InsufficientFunds/InvalidOrder → return None (no retry)
5. On DDoSProtection → safe to retry (order was NOT placed)
```

### Order Placement Sequence (per trade)

```
1. exchange.set_leverage(leverage, symbol)
2. Place entry order (market or limit) → get fill
3. Verify fill via separate GET call (anti-hallucination Layer 5)
4. Place STOP_MARKET (SL) with reduceOnly=True
5. Place TAKE_PROFIT_MARKET (TP) with reduceOnly=True
6. Verify SL/TP orders are active
7. Initialize TrailingStopState
```

---

## 3. Fee Structure

| Scenario | Fee Rate | Calculation |
|----------|----------|-------------|
| Maker (limit/GTX) | 0.02% | 0.0002 × notional |
| Taker (market) | 0.05% | 0.0005 × notional |
| Round-trip (maker+taker) | 0.07% | Entry maker + exit taker |
| Round-trip (taker both) | 0.10% | Both sides market |
| BNB Burn discount | 10% off | ENABLED on account |

**Always prefer Post-Only (GTX) limit orders for entries → 0.02% vs 0.05%.**

---

## 4. Funding Rate

- Interval: **Every 8 hours** (00:00, 08:00, 16:00 UTC)
- Current rates: ETH ~0.0029%, SOL ~0.0045%, DOGE ~0.01%
- Impact: 0.01% per 8h ≈ 0.03%/day ≈ 1%/month drag
- Factor into hold-time decisions for multi-hour positions

---

## 5. Liquidation & Mark Price

- Binance liquidates on **MARK PRICE** (not last price)
- Heuristic: liquidation distance ≈ `1 / leverage`
- **Authoritative check**: `exchange.fetch_positions()` → `liquidationPrice`
- Rule: SL must be < 50% of actual liquidation distance
- Immutable Rule #10: entry-to-liquidation must be ≥ 5%

| Leverage | Approx Distance | Safety |
|----------|-----------------|--------|
| 3x | ~33% | Safe ✅ |
| 5x | ~20% | Default ✅ |
| 7x | ~14% | BTC/ETH only ⚠️ |
| 10x | ~10% | Max leverage, ≥85 confidence ⚠️ |

---

## 6. WebSocket & Listen Key Management

| Constraint | Value |
|-----------|-------|
| Listen key expiry | 60 minutes |
| Connection validity | 24 hours max |
| Keep-alive interval | ≤ 30 minutes recommended |

If bypassing ccxt for custom WebSocket tooling:
1. Use correct testnet/production URLs
2. PUT `/fapi/v1/listenKey` every 30 min
3. Handle 24h reconnection
4. Separate API keys per environment

---

## 7. Position Reconciliation (Every 5 Minutes)

```python
# Pattern from orchestrator
1. positions = await exchange.fetch_positions()
2. Compare with local position_tracker state
3. Mismatch → BINANCE STATE is truth → update local
4. Phantom positions (on Binance, not local) → close immediately
5. Missing SL/TP → place immediately
6. Log all reconciliation as WARNING
```

---

## 8. Error Handling Matrix

| Error | Action | Retry? |
|-------|--------|--------|
| `NetworkError` / timeout | Query by clientOrderId, then retry with new ID | Yes (max 3) |
| `InsufficientFunds` | Return None, log, alert | No |
| `InvalidOrder` | Return None, log, check params | No |
| `DDoSProtection` | Wait + retry (order definitely not placed) | Yes |
| `ExchangeNotAvailable` | Wait exponential backoff | Yes (max 3) |
| HTTP 503 | Order MAY have succeeded — query first | Check then retry |

---

## 9. Key Code Files

| File | Purpose |
|------|---------|
| `src/execution/order_manager.py` | All order placement + idempotency |
| `src/execution/fee_calculator.py` | Fee computation |
| `src/execution/position_tracker.py` | Position state tracking |
| `src/execution/slippage_estimator.py` | Execution quality |
| `src/data/market_data.py` | OHLCV + ticker data fetching |
| `src/orchestrator/main.py` | 7-step cycle (Steps 2, 6) |
| `config/risk/risk_params.yaml` | Max daily trades, pair config |

---

## 10. Anti-Patterns (NEVER DO)

- ❌ Use `set_sandbox_mode(True)` — routes to dead endpoints
- ❌ Submit orders without `newClientOrderId`
- ❌ Retry on timeout without checking if order exists
- ❌ Mix testnet/mainnet API keys
- ❌ Place SL/TP without `reduceOnly=True`
- ❌ Use `BTCUSDT` format instead of `BTC/USDT:USDT`
- ❌ Report "order filled" without GET verification
- ❌ Skip leverage setting before order placement
- ❌ Ignore funding rate in hold-time calculations
