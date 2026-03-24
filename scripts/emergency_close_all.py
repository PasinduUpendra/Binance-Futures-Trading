"""
Emergency: Close ALL open positions and cancel ALL open orders.

Use this to reset the exchange state before restarting the bot.
Run: python scripts/emergency_close_all.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()

import ccxt.async_support as ccxt_async


SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"]


async def main() -> None:
    exchange = ccxt_async.binanceusdm(
        {
            "apiKey": os.getenv("BINANCE_API_KEY", ""),
            "secret": os.getenv("BINANCE_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
        }
    )

    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    if testnet:
        exchange.enable_demo_trading(True)
        print("Connected to TESTNET")
    else:
        print("Connected to PRODUCTION — be careful!")

    await exchange.load_markets()

    # 1. Cancel all open orders on all symbols
    print("\n=== Cancelling all open orders ===")
    for sym in SYMBOLS:
        try:
            orders = await exchange.fetch_open_orders(sym)
            if orders:
                for order in orders:
                    await exchange.cancel_order(order["id"], sym)
                    print(f"  Cancelled {sym} order {order['id']} ({order['type']})")
            else:
                print(f"  {sym}: no open orders")
        except Exception as e:
            print(f"  {sym}: error cancelling orders: {e}")

    # 2. Close all open positions
    print("\n=== Closing all open positions ===")
    try:
        positions = await exchange.fetch_positions()
        open_positions = [
            p for p in positions
            if abs(float(p.get("contracts", 0))) > 0
        ]

        if not open_positions:
            print("  No open positions found")
        else:
            for pos in open_positions:
                sym = pos["symbol"]
                contracts = abs(float(pos["contracts"]))
                side = pos.get("side", "").lower()
                close_side = "sell" if side == "long" else "buy"

                print(
                    f"  Closing {sym} {side} {contracts} contracts "
                    f"(unrealized PnL: {pos.get('unrealizedPnl', '?')})"
                )

                try:
                    result = await exchange.create_order(
                        symbol=sym,
                        type="market",
                        side=close_side,
                        amount=contracts,
                        params={"reduceOnly": True},
                    )
                    print(f"    -> Closed. Order ID: {result['id']}")
                except Exception as e:
                    print(f"    -> FAILED: {e}")

    except Exception as e:
        print(f"  Error fetching positions: {e}")

    # 3. Final balance check
    print("\n=== Final State ===")
    try:
        balance = await exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        print(f"  Total: ${usdt.get('total', '?')}")
        print(f"  Free:  ${usdt.get('free', '?')}")
        print(f"  Used:  ${usdt.get('used', '?')}")
    except Exception as e:
        print(f"  Error fetching balance: {e}")

    await exchange.close()
    print("\nDone. All positions closed, all orders cancelled.")


if __name__ == "__main__":
    asyncio.run(main())
