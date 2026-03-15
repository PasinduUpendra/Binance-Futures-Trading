---
name: ccxt-crypto-integration
description: >
  CCXT async crypto exchange integration patterns. Use when: writing exchange API calls, handling ccxt errors,
  managing rate limits, fetching OHLCV/positions/orders, setting leverage/margin mode, async exchange patterns,
  WebSocket subscriptions, or debugging ccxt-specific issues with Binance USDT-M Futures.
applyTo: "src/execution/**,src/data/**,src/orchestrator/**,scripts/**"
---

# CCXT Async Crypto Exchange Integration — Comprehensive Skill

## Scope

This skill covers all `ccxt.async_support.binanceusdm` usage patterns, error handling, async lifecycle,
data fetching, and integration with the Claude Quant trading system.

---

## 1. Exchange Initialization

```python
import ccxt.async_support as ccxt_async

async def create_exchange(testnet: bool = True) -> ccxt_async.binanceusdm:
    exchange = ccxt_async.binanceusdm({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,          # MANDATORY — prevents 429 bans
        'options': {
            'adjustForTimeDifference': True,
            'defaultType': 'future',
            'recvWindow': 10000,          # 10s tolerance for clock skew
        },
    })

    if testnet:
        exchange.enable_demo_trading(True)  # ✅ Correct method
        # ❌ NEVER: exchange.set_sandbox_mode(True)  — routes to DEAD endpoints

    return exchange
```

### Lifecycle Management (CRITICAL)

```python
# ALWAYS close exchange when done — prevents connection leaks
exchange = await create_exchange()
try:
    # ... trading operations ...
finally:
    await exchange.close()

# In async context managers:
async with exchange_context() as exchange:
    # ... operations ...
# exchange.close() called automatically
```

---

## 2. Data Fetching Patterns

### OHLCV (Candlestick) Data

```python
# Fetch 200 4H candles for ETH/USDT:USDT
ohlcv = await exchange.fetch_ohlcv(
    symbol='ETH/USDT:USDT',
    timeframe='4h',
    limit=200
)
# Returns: [[timestamp_ms, open, high, low, close, volume], ...]

# Convert to DataFrame:
import pandas as pd
df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
```

### Ticker / Current Price

```python
ticker = await exchange.fetch_ticker('ETH/USDT:USDT')
# ticker['last']    → last trade price
# ticker['bid']     → best bid
# ticker['ask']     → best ask
# ticker['mark']    → mark price (for liquidation)
```

### Positions

```python
positions = await exchange.fetch_positions(['ETH/USDT:USDT'])
# Returns list of position dicts
# Key fields: 'symbol', 'side', 'contracts', 'entryPrice',
#             'unrealizedPnl', 'liquidationPrice', 'leverage', 'marginType'
```

### Open Orders

```python
orders = await exchange.fetch_open_orders('ETH/USDT:USDT')
# Returns list of order dicts
# Key fields: 'id', 'clientOrderId', 'type', 'side', 'price',
#             'amount', 'status', 'stopPrice'
```

### Account Balance

```python
balance = await exchange.fetch_balance()
# balance['USDT']['total']   → total balance
# balance['USDT']['free']    → available margin
# balance['USDT']['used']    → margin in use
```

---

## 3. Order Placement

### Market Order

```python
order = await exchange.create_order(
    symbol='ETH/USDT:USDT',
    type='market',
    side='buy',       # 'buy' for LONG, 'sell' for SHORT
    amount=0.05,      # in base currency (ETH)
    params={
        'newClientOrderId': f'cq_{uuid4().hex[:16]}',  # REQUIRED for idempotency
    }
)
```

### Limit Order (Post-Only / GTX)

```python
order = await exchange.create_order(
    symbol='ETH/USDT:USDT',
    type='limit',
    side='buy',
    amount=0.05,
    price=3400.00,
    params={
        'newClientOrderId': f'cq_{uuid4().hex[:16]}',
        'timeInForce': 'GTX',  # Post-Only — guaranteed maker fee (0.02%)
    }
)
```

### Stop-Loss (STOP_MARKET)

```python
order = await exchange.create_order(
    symbol='ETH/USDT:USDT',
    type='STOP_MARKET',    # ccxt passes through to Binance
    side='sell',           # Opposite of position direction
    amount=0.05,
    params={
        'stopPrice': 3300.00,
        'reduceOnly': True,         # MANDATORY for protective orders
        'newClientOrderId': f'cq_{uuid4().hex[:16]}',
    }
)
```

### Take-Profit (TAKE_PROFIT_MARKET)

```python
order = await exchange.create_order(
    symbol='ETH/USDT:USDT',
    type='TAKE_PROFIT_MARKET',
    side='sell',
    amount=0.05,
    params={
        'stopPrice': 3600.00,
        'reduceOnly': True,
        'newClientOrderId': f'cq_{uuid4().hex[:16]}',
    }
)
```

### Set Leverage

```python
await exchange.set_leverage(5, 'ETH/USDT:USDT')
# Must be called BEFORE placing orders
# Max 10x (Immutable Rule #2)
```

### Set Margin Mode

```python
await exchange.set_margin_mode('isolated', 'ETH/USDT:USDT')
# This project uses ISOLATED margin exclusively
```

---

## 4. Error Handling (Comprehensive)

```python
import ccxt

try:
    order = await exchange.create_order(...)
except ccxt.InsufficientFunds as e:
    # Balance too low for this trade
    # Action: return None, do NOT retry
    logger.error(f"Insufficient funds: {e}")
    return None

except ccxt.InvalidOrder as e:
    # Invalid parameters (amount, price, etc.)
    # Action: return None, check params
    logger.error(f"Invalid order: {e}")
    return None

except ccxt.DDoSProtection as e:
    # Rate limited — order was definitely NOT placed
    # Action: safe to retry after backoff
    await asyncio.sleep(backoff)

except ccxt.ExchangeNotAvailable as e:
    # Exchange down or maintenance
    # Action: retry with exponential backoff
    await asyncio.sleep(backoff)

except ccxt.NetworkError as e:
    # Timeout, connection reset — order MAY have been placed
    # Action: query by clientOrderId FIRST, then decide
    existing = await query_by_client_order_id(symbol, client_oid)
    if existing:
        return existing  # Order exists, no duplicate
    # else: retry with NEW clientOrderId

except ccxt.AuthenticationError as e:
    # API key issue — HALT
    logger.critical(f"Auth error: {e}")
    raise

except ccxt.ExchangeError as e:
    # Generic exchange error — log and investigate
    logger.error(f"Exchange error: {e}")
```

### Rate Limit Handling

```python
# ccxt handles basic rate limiting when enableRateLimit=True
# For burst scenarios, add manual backoff:
exchange.rateLimit = 100  # milliseconds between calls (Binance: 1200 req/min)

# Monitor rate limit headers:
# exchange.last_response_headers['X-MBX-USED-WEIGHT-1M']
```

---

## 5. Async Patterns for Claude Quant

### Parallel Data Fetching

```python
import asyncio

async def fetch_all_pairs(exchange, pairs, timeframe='4h', limit=200):
    """Fetch OHLCV for all pairs concurrently."""
    tasks = [
        exchange.fetch_ohlcv(pair, timeframe, limit=limit)
        for pair in pairs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return dict(zip(pairs, results))
```

### Exchange Context Manager

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def exchange_session(testnet=True):
    exchange = await create_exchange(testnet)
    try:
        yield exchange
    finally:
        await exchange.close()
```

### Querying Order by Client ID

```python
async def query_by_client_order_id(exchange, symbol, client_oid):
    """Query Binance for an order by its clientOrderId."""
    await asyncio.sleep(2)  # Propagation delay
    try:
        result = await exchange.fapiPrivateGetOrder({
            'symbol': exchange.market_id(symbol),
            'origClientOrderId': client_oid,
        })
        return result
    except ccxt.OrderNotFound:
        return None
```

---

## 6. ccxt-Specific Gotchas

| Issue | Cause | Fix |
|-------|-------|-----|
| `set_sandbox_mode()` fails | Deprecated, routes to dead URLs | Use `enable_demo_trading(True)` |
| Symbol `ETHUSDT` fails | Wrong format | Use `ETH/USDT:USDT` |
| Stale ticker data | Rate limit caching | Ensure `enableRateLimit=True` |
| Position `contracts=0` | No position open | Filter out zero-size positions |
| `fetchBalance` shows 0 | Wrong account type | Set `defaultType: 'future'` |
| Leverage not applied | Set after order | Call `set_leverage()` BEFORE order |
| Stop order ignored | Missing `stopPrice` | Pass in `params.stopPrice` |
| Duplicate orders on retry | No clientOrderId dedup | Always use `newClientOrderId` |

---

## 7. Data Validation After Fetch

```python
def validate_ohlcv(df: pd.DataFrame) -> bool:
    """Anti-hallucination Layer 1 checks."""
    # 1. Not empty
    if df.empty:
        return False
    # 2. No NaN in OHLCV columns
    if df[['open', 'high', 'low', 'close', 'volume']].isna().any().any():
        return False
    # 3. low <= high for all rows
    if (df['low'] > df['high']).any():
        return False
    # 4. Timestamps are monotonically increasing
    if not df['timestamp'].is_monotonic_increasing:
        return False
    # 5. Freshness: last candle within 2 candle intervals
    return True
```

---

## 8. Key Code Files

| File | ccxt Usage |
|------|-----------|
| `src/data/market_data.py` | OHLCV fetch, ticker, exchange init |
| `src/execution/order_manager.py` | All order CRUD, idempotent submit |
| `src/execution/position_tracker.py` | Position fetch + reconciliation |
| `src/orchestrator/main.py` | Exchange lifecycle, balance fetch |
| `src/execution/fee_calculator.py` | Fee rate lookups |
| `scripts/backtest_v4.py` | Offline (no exchange calls) |

---

## 9. Testing with ccxt

```python
# For unit tests, mock the exchange:
from unittest.mock import AsyncMock, MagicMock

mock_exchange = MagicMock()
mock_exchange.fetch_ohlcv = AsyncMock(return_value=[[...]])
mock_exchange.create_order = AsyncMock(return_value={'id': '123', 'status': 'filled'})
mock_exchange.fetch_positions = AsyncMock(return_value=[])
mock_exchange.fetch_balance = AsyncMock(return_value={'USDT': {'total': 5000, 'free': 5000}})
mock_exchange.close = AsyncMock()
```
