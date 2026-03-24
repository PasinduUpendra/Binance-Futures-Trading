#!/usr/bin/env python3
"""Check available pairs on Binance testnet."""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
import ccxt.async_support as ccxt


async def main():
    ex = ccxt.binanceusdm({
        "apiKey": os.getenv("BINANCE_TESTNET_API_KEY"),
        "secret": os.getenv("BINANCE_TESTNET_API_SECRET"),
        "enableRateLimit": True,
        "options": {"adjustForTimeDifference": True, "defaultType": "future"},
    })
    ex.enable_demo_trading(True)
    await ex.load_markets()

    targets = [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
        "XRP/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
        "LINK/USDT:USDT", "DOT/USDT:USDT", "MATIC/USDT:USDT",
        "WIF/USDT:USDT", "PEPE/USDT:USDT", "ARB/USDT:USDT",
        "SUI/USDT:USDT", "OP/USDT:USDT",
    ]

    print(f"Total futures markets: {len([s for s in ex.markets if ':USDT' in s])}")
    print()
    for sym in targets:
        if sym in ex.markets:
            m = ex.markets[sym]
            limits = m.get("limits", {})
            cost_min = limits.get("cost", {}).get("min", "N/A")
            amount_prec = m.get("precision", {}).get("amount", "N/A")
            price_prec = m.get("precision", {}).get("price", "N/A")
            print(f"  {sym}: minCost={cost_min}, amtPrec={amount_prec}, prcPrec={price_prec}")
        else:
            print(f"  {sym}: NOT AVAILABLE")

    await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
