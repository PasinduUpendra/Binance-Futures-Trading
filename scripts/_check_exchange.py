"""Quick exchange state check — temporary script."""
import asyncio
import sys
sys.path.insert(0, '.')
from src.data.market_data import MarketDataClient

async def check():
    client = MarketDataClient()
    await client.connect()
    exchange = client._require_exchange()
    
    # Balance
    balance = await client.get_margin_balance()
    print(f'BALANCE: {balance} USDT')
    
    # Positions
    positions = await exchange.fetch_positions()
    open_pos = [p for p in positions if float(p['contracts']) > 0]
    print(f'OPEN_POSITIONS: {len(open_pos)}')
    for p in open_pos:
        side = 'LONG' if p['side'] == 'long' else 'SHORT'
        pnl = float(p.get('unrealizedPnl', 0))
        entry = float(p.get('entryPrice', 0))
        mark = float(p.get('markPrice', 0))
        lev = p.get('leverage', '?')
        print(f'  {p["symbol"]} {side} entry={entry} mark={mark} lev={lev}x pnl={pnl:.2f}')
    
    # Open orders (including conditional/algo)
    for p in open_pos:
        sym = p["symbol"]
        # Regular orders
        orders = await exchange.fetch_open_orders(sym)
        # Conditional orders (SL/TP are algo orders on Binance)
        try:
            cond = await exchange.fapiPrivateGetOpenOrders({'symbol': exchange.market_id(sym)})
        except Exception:
            cond = []
        # Also try fetching all open orders
        try:
            all_orders = await exchange.fapiPrivateV2GetOpenOrders({'symbol': exchange.market_id(sym)})
        except Exception:
            all_orders = []
        print(f'  {sym} ccxt_orders={len(orders)} raw_orders={len(cond)} v2_orders={len(all_orders)}')
        for o in orders:
            print(f'    [ccxt] {o["type"]} {o["side"]} trigger={o.get("stopPrice","?")} status={o["status"]}')
        for o in cond:
            print(f'    [raw] type={o.get("type","?")} side={o.get("side","?")} stopPrice={o.get("stopPrice","?")} origType={o.get("origType","?")}')
        for o in all_orders:
            print(f'    [v2] type={o.get("type","?")} side={o.get("side","?")} stopPrice={o.get("stopPrice","?")} origType={o.get("origType","?")}')
    
    await exchange.close()

asyncio.run(check())
