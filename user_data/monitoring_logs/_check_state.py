"""Quick exchange state check."""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
import ccxt.async_support as ccxt_async

async def check():
    ex = ccxt_async.binanceusdm({
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_API_SECRET"),
        "enableRateLimit": True,
    })
    ex.enable_demo_trading(True)
    
    positions = [p for p in await ex.fetch_positions() if float(p["contracts"]) > 0]
    for p in positions:
        sym = p["symbol"]
        print(f"Position: {sym} {p['side']} size={p['contracts']} entry={p['entryPrice']} uPnL={p['unrealizedPnl']}")
        orders = await ex.fetch_open_orders(sym, params={"trigger": True})
        print(f"  Conditional orders: {len(orders)}")
        for o in orders:
            print(f"    id={o['id']} type={o['type']} side={o['side']} stopPrice={o.get('stopPrice')}")
    
    if not positions:
        print("No open positions.")
    
    bal = await ex.fetch_balance()
    print(f"\nBalance: total={bal['total']['USDT']}, free={bal['free']['USDT']}")
    await ex.close()

asyncio.run(check())
