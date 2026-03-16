"""
Download BTC/USDT:USDT historical data for parameter sweep validation.

Fetches 1H and 4H candles for BTC and saves them in the same format
as the existing pair data files in user_data/data/.

Usage: python scripts/download_btc_data.py
"""

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_data import MarketDataClient

DATA_DIR = PROJECT_ROOT / "user_data" / "data"
SYMBOL = "BTC/USDT:USDT"
TIMEFRAMES = ["1h", "4h"]
LIMIT = 1500  # Max candles per request


async def download() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with MarketDataClient() as client:
        for tf in TIMEFRAMES:
            print(f"Fetching {SYMBOL} {tf} ({LIMIT} candles)...")
            candles = await client.fetch_ohlcv(SYMBOL, tf, limit=LIMIT)

            # Convert to serializable format matching existing files
            data = []
            for c in candles:
                data.append({
                    "timestamp": int(c["timestamp"].timestamp() * 1000),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c["volume"]),
                })

            filename = f"BTC_USDT_USDT_{tf}.json"
            path = DATA_DIR / filename
            path.write_text(json.dumps(data, indent=2))
            print(f"  Saved {len(data)} candles to {path}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(download())
