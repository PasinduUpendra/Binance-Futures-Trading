"""
Download historical OHLCV data for new trading pairs using ccxt.
Saves in the same format as existing data files.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ccxt.async_support as ccxt_async

# New pairs to download (testnet-confirmed available)
NEW_PAIRS = [
    "BTC/USDT:USDT",
    "XRP/USDT:USDT",
    "LINK/USDT:USDT",
    "AVAX/USDT:USDT",
    "SUI/USDT:USDT",
    "ADA/USDT:USDT",
]

TIMEFRAMES = ["1h", "4h"]
DATA_DIR = PROJECT_ROOT / "user_data" / "data"

# Match existing data range: Sep 14, 2025 - Mar 13, 2026
START_TS = 1757887200000  # 2025-09-14 22:00:00 UTC
END_TS = 1773435600000    # 2026-03-13 21:00:00 UTC


async def fetch_ohlcv_all(
    exchange: ccxt_async.binanceusdm,
    symbol: str,
    timeframe: str,
    since: int,
    until: int,
) -> list[dict]:
    """Fetch all OHLCV candles in the given range, paginating as needed."""
    all_candles = []
    current_since = since
    limit = 1500  # Binance max per request

    while current_since < until:
        candles = await exchange.fetch_ohlcv(
            symbol, timeframe, since=current_since, limit=limit
        )
        if not candles:
            break

        for c in candles:
            ts, o, h, l, cl, v = c
            if ts > until:
                break
            all_candles.append({
                "timestamp": ts,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(cl),
                "volume": float(v),
            })

        last_ts = candles[-1][0]
        if last_ts <= current_since:
            break
        current_since = last_ts + 1

        # Rate limit respect
        await asyncio.sleep(0.2)

    # Deduplicate by timestamp
    seen = set()
    unique = []
    for c in all_candles:
        if c["timestamp"] not in seen and c["timestamp"] <= until:
            seen.add(c["timestamp"])
            unique.append(c)

    unique.sort(key=lambda x: x["timestamp"])
    return unique


async def main() -> None:
    exchange = ccxt_async.binanceusdm({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    try:
        await exchange.load_markets()

        for symbol in NEW_PAIRS:
            if symbol not in exchange.markets:
                print(f"  SKIP {symbol}: not available on Binance Futures")
                continue

            for tf in TIMEFRAMES:
                pair_file = symbol.replace("/", "_").replace(":", "_")
                output_path = DATA_DIR / f"{pair_file}_{tf}.json"

                if output_path.exists():
                    print(f"  SKIP {symbol} {tf}: already exists at {output_path}")
                    continue

                print(f"  Downloading {symbol} {tf}...", end=" ", flush=True)
                candles = await fetch_ohlcv_all(exchange, symbol, tf, START_TS, END_TS)
                print(f"{len(candles)} candles")

                output_path.write_text(json.dumps(candles, indent=None))

        print("\nDone!")

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
