#!/usr/bin/env python3
"""Quick script to check real Binance balance using PROD keys."""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

import ccxt.async_support as ccxt_async


async def main() -> None:
    api_key = os.getenv("BINANCE_API_KEY_PROD")
    api_secret = os.getenv("BINANCE_API_SECRET_PROD")

    if not api_key or not api_secret:
        print("ERROR: BINANCE_API_KEY_PROD or BINANCE_API_SECRET_PROD not set")
        return

    exchange = ccxt_async.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
        }
    )

    try:
        balance = await exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        total = usdt.get("total", 0)
        free = usdt.get("free", 0)
        used = usdt.get("used", 0)
        print(f"USDT Total: {total}")
        print(f"USDT Free:  {free}")
        print(f"USDT Used:  {used}")

        positions = await exchange.fetch_positions()
        open_pos = [p for p in positions if float(p.get("contracts", 0)) > 0]
        if open_pos:
            print(f"\nOpen positions: {len(open_pos)}")
            for p in open_pos:
                print(
                    f"  {p['symbol']} {p['side']} {p['contracts']} contracts, "
                    f"PnL: {p.get('unrealizedPnl', '?')}"
                )
        else:
            print("\nNo open positions.")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
