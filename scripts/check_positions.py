"""Quick script to check current positions and orders on exchange."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.data.market_data import MarketDataClient


async def check():
    md = MarketDataClient()
    await md.connect()

    balance = await md.get_account_balance()
    print(f"BALANCE: ${balance}")

    positions = await md._exchange.fetch_positions()
    for p in positions:
        if float(p["contracts"]) > 0:
            print(f"\nPOSITION: {p['symbol']}")
            print(f"  Side: {p['side']}")
            print(f"  Contracts: {p['contracts']}")
            print(f"  Entry: {p['entryPrice']}")
            print(f"  Mark: {p['markPrice']}")
            print(f"  Unrealized PnL: {p['unrealizedPnl']}")
            print(f"  Notional: {p['notional']}")
            print(f"  Leverage: {p['leverage']}")
            print(f"  Liq Price: {p['liquidationPrice']}")
            print(f"  Margin: {p['initialMargin']}")

    for sym in ["SOL/USDT:USDT", "ETH/USDT:USDT", "DOGE/USDT:USDT"]:
        orders = await md._exchange.fetch_open_orders(sym)
        print(f"\nOPEN ORDERS for {sym}: {len(orders)}")
        for o in orders:
            stop = o.get("stopPrice") or o.get("price")
            print(f"  {o['type']} {o['side']} qty={o['amount']} stopPrice={stop} reduceOnly={o['info'].get('reduceOnly')}")

    # Get recent trades/fills
    for sym in ["SOL/USDT:USDT", "ETH/USDT:USDT", "DOGE/USDT:USDT"]:
        trades = await md._exchange.fetch_my_trades(sym, limit=20)
        print(f"\nRECENT FILLS for {sym}: {len(trades)}")
        for t in trades[-10:]:
            print(f"  {t['datetime']} {t['side']} qty={t['amount']} price={t['price']} cost={t['cost']} fee={t['fee']}")

    await md.close()


if __name__ == "__main__":
    asyncio.run(check())
