---
name: trade-execution
description: >
  Execute approved trades on Binance USDT-M Futures with idempotent order submission,
  proper SL/TP placement, fill verification, and error handling.
  Full reference: .github/skills/binance-futures-trading/SKILL.md
---

# Trade Execution Skill

Place and manage trades on Binance Futures using the production order pipeline.

## Prerequisites
- Risk Manager MUST have approved the trade (Step 4 of orchestrator)
- Circuit breaker must allow trading (not DEAD)

## Execution Steps (from `order_manager.py`)
1. Verify Risk Manager approval exists
2. Set leverage for the pair: `exchange.set_leverage(lev, symbol)`
3. Calculate order quantity: `notional / entry_price`
4. Place entry order with idempotent `newClientOrderId` (cq_<uuid16>)
5. Verify fill via separate GET call (anti-hallucination Layer 5)
6. Place STOP_MARKET (SL) with `reduceOnly=True`
7. Place TAKE_PROFIT_MARKET (TP) with `reduceOnly=True`
8. Verify all orders are active via GET
9. Initialize TrailingStopState for the new position
10. Log execution details to trade journal

## Idempotent Order Pattern
```
1. Generate unique client_order_id = f"cq_{uuid4().hex[:16]}"
2. Submit order with newClientOrderId
3. On NetworkError/timeout → query by clientOrderId first
4. On InsufficientFunds/InvalidOrder → return None (no retry)
5. On DDoSProtection → safe to retry (order not placed)
6. Max 3 attempts, exponential backoff (2s, 4s)
```

## Safety Rules
- ❌ NEVER skip Risk Manager approval (step 1)
- ❌ NEVER place SL/TP without reduceOnly=True
- ❌ NEVER report "filled" without GET verification
- ❌ NEVER retry on timeout without checking if order exists
- ❌ NEVER use set_sandbox_mode — use enable_demo_trading
- ✅ ALWAYS verify fill price within 0.5% of expected
- ✅ ALWAYS set leverage BEFORE placing orders
- ✅ ALWAYS use correct symbol format: `ETH/USDT:USDT`

## Order Types
| Type | Binance Type | Purpose |
|------|-------------|---------|
| Market | `MARKET` | Entry (taker 0.05%) |
| Limit GTX | `LIMIT` + `GTX` | Entry (maker 0.02%) — preferred |
| Stop-Loss | `STOP_MARKET` | Protective SL, reduceOnly |
| Take-Profit | `TAKE_PROFIT_MARKET` | TP exit, reduceOnly |

## Full Reference
See `.github/skills/binance-futures-trading/SKILL.md` and `.github/skills/ccxt-crypto-integration/SKILL.md`.
