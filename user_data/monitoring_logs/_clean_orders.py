"""Clean duplicate conditional orders on LINK and ADA, then verify."""
import asyncio
import os
import ccxt.async_support as ccxt_async
from dotenv import load_dotenv

load_dotenv()

SYMBOLS = ["LINK/USDT:USDT", "ADA/USDT:USDT"]

async def main():
    ex = ccxt_async.binanceusdm({
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_API_SECRET"),
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    ex.enable_demo_trading(True)

    # 1. Show current state
    print("=== BEFORE CLEANUP ===")
    for sym in SYMBOLS:
        orders = await ex.fetch_open_orders(sym, params={"trigger": True})
        print(f"  {sym}: {len(orders)} conditional orders")

    # 2. Cancel all conditional orders
    print("\n=== CANCELLING ===")
    for sym in SYMBOLS:
        try:
            result = await ex.cancel_all_orders(sym, params={"trigger": True})
            print(f"  {sym}: cancel_all_orders done")
        except Exception as e:
            print(f"  {sym}: cancel_all_orders error: {e}")
            # Fallback: cancel individually
            orders = await ex.fetch_open_orders(sym, params={"trigger": True})
            for o in orders:
                try:
                    await ex.cancel_order(o["id"], sym)
                except Exception as e2:
                    print(f"    Failed to cancel {o['id']}: {e2}")

    await asyncio.sleep(2)  # Let Binance propagate

    # 3. Verify
    print("\n=== AFTER CLEANUP ===")
    for sym in SYMBOLS:
        orders = await ex.fetch_open_orders(sym, params={"trigger": True})
        print(f"  {sym}: {len(orders)} conditional orders")

    # 4. Show current positions
    print("\n=== OPEN POSITIONS ===")
    positions = await ex.fetch_positions()
    for p in positions:
        if abs(float(p.get("contracts", 0))) > 0:
            print(f"  {p['symbol']} {p['side']} size={p['contracts']} entry={p['entryPrice']} uPnL={p.get('unrealizedPnl', 'N/A')}")

    # 5. Balance
    balance = await ex.fetch_balance()
    total = float(balance.get("total", {}).get("USDT", 0))
    free = float(balance.get("free", {}).get("USDT", 0))
    print(f"\nBalance: total={total:.2f} free={free:.2f}")

    # Also check ALL symbols for lingering orders
    print("\n=== ALL SYMBOLS ORDER CHECK ===")
    all_pairs = [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
        "DOGE/USDT:USDT", "XRP/USDT:USDT", "LINK/USDT:USDT",
        "AVAX/USDT:USDT", "SUI/USDT:USDT", "ADA/USDT:USDT"
    ]
    for sym in all_pairs:
        orders = await ex.fetch_open_orders(sym, params={"trigger": True})
        if orders:
            print(f"  {sym}: {len(orders)} orders remaining!")
        else:
            print(f"  {sym}: clean (0 orders)")

    await ex.close()
    print("\nDone.")

asyncio.run(main())
