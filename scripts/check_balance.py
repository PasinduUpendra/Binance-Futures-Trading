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
    # Check bot process
    import subprocess
    import datetime

    result = subprocess.run(
        ["pgrep", "-f", "run_bot.py"], capture_output=True, text=True
    )
    pids = result.stdout.strip()
    if pids:
        pid = pids.split("\n")[0]
        ps = subprocess.run(["ps", "-o", "etime=", "-p", pid], capture_output=True, text=True)
        uptime = ps.stdout.strip()
        print(f"Bot PID: {pid} (uptime: {uptime})")
    else:
        print("WARNING: Bot process NOT running!")

    print(f"Check time: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()

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
                    f"PnL: {p.get('unrealizedPnl', '?')}, entry: {p.get('entryPrice', '?')}"
                )
                # Show conditional/trigger orders (SL/TP)
                sym = p["symbol"]
                trigger_orders = await exchange.fetch_open_orders(
                    sym, params={"trigger": True}
                )
                if trigger_orders:
                    for o in trigger_orders:
                        print(
                            f"    -> {o.get('type','?')} {o['side']} "
                            f"stopPrice={o.get('stopPrice','?')} status={o['status']}"
                        )
                else:
                    print("    -> WARNING: NO SL/TP orders!")
        else:
            print("\nNo open positions.")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
