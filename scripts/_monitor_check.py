#!/usr/bin/env python3
"""One-shot monitoring check - queries real Binance data."""
import asyncio, os, sys, datetime
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / '.env', override=True)
sys.path.insert(0, str(Path(__file__).parent.parent))
import ccxt.async_support as ccxt_async

async def check():
    ex = ccxt_async.binanceusdm({
        'apiKey': os.getenv('BINANCE_API_KEY_PROD'),
        'secret': os.getenv('BINANCE_API_SECRET_PROD'),
        'enableRateLimit': True,
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        print(f"CHECK TIME: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        # Bot process
        import subprocess
        result = subprocess.run(["pgrep", "-f", "run_bot.py"], capture_output=True, text=True)
        pids = result.stdout.strip()
        if pids:
            pid = pids.split("\n")[0]
            ps = subprocess.run(["ps", "-o", "etime=", "-p", pid], capture_output=True, text=True)
            print(f"BOT: PID {pid}, uptime {ps.stdout.strip()}")
        else:
            print("BOT: NOT RUNNING!")

        # Balance
        bal = await ex.fetch_balance()
        usdt = bal.get('USDT', {})
        print(f"\n=== BALANCE ===")
        print(f"Total: ${usdt.get('total', 0)}")
        print(f"Free:  ${usdt.get('free', 0)}")
        print(f"Used:  ${usdt.get('used', 0)}")

        # Positions
        positions = await ex.fetch_positions()
        open_pos = [p for p in positions if abs(float(p.get('contracts', 0))) > 0]
        print(f"\n=== POSITIONS ({len(open_pos)}) ===")
        for p in open_pos:
            sym = p['symbol']
            print(f"  {sym} | {p['side']} | qty={p['contracts']} | entry={p['entryPrice']} | mark={p['markPrice']} | uPnL={p['unrealizedPnl']} | lev={p['leverage']} | liq={p['liquidationPrice']}")
            trigger_orders = await ex.fetch_open_orders(sym, params={'trigger': True})
            regular_orders = await ex.fetch_open_orders(sym)
            if trigger_orders:
                for o in trigger_orders:
                    print(f"    SL/TP: {o.get('type','?')} {o['side']} stop={o.get('stopPrice','?')} reduce={o['info'].get('reduceOnly','?')}")
            else:
                print(f"    WARNING: NO SL/TP ORDERS - POSITION IS NAKED!")
            if regular_orders:
                for o in regular_orders:
                    print(f"    ORDER: {o.get('type','?')} {o['side']} price={o.get('price','?')}")

        # Recent trades 24h
        since_24h = int((now - datetime.timedelta(hours=24)).timestamp() * 1000)
        pairs = ['BTC/USDT:USDT','ETH/USDT:USDT','SOL/USDT:USDT','DOGE/USDT:USDT','XRP/USDT:USDT','LINK/USDT:USDT','AVAX/USDT:USDT','SUI/USDT:USDT','ADA/USDT:USDT']
        print(f"\n=== TRADES (last 24h) ===")
        total_trades = 0
        for sym in pairs:
            try:
                trades = await ex.fetch_my_trades(sym, since=since_24h, limit=50)
                if trades:
                    total_trades += len(trades)
                    for t in trades:
                        print(f"  {t['datetime']} {sym} {t['side']} qty={t['amount']} price={t['price']} cost=${t['cost']:.2f} fee={t['fee']}")
            except:
                pass
        print(f"Total fills: {total_trades}")

        # Realized PnL
        print(f"\n=== REALIZED PNL (last 24h) ===")
        try:
            income = await ex.fapiPrivateGetIncome({'incomeType': 'REALIZED_PNL', 'startTime': since_24h, 'limit': 50})
            total_pnl = 0.0
            for i in income:
                ts = datetime.datetime.fromtimestamp(int(i['time'])/1000, tz=datetime.timezone.utc).strftime('%H:%M:%S')
                total_pnl += float(i['income'])
                print(f"  {ts} {i['symbol']} PnL={i['income']}")
            print(f"  TOTAL realized PnL: ${total_pnl:.4f}")
        except Exception as e:
            print(f"  Error: {e}")

        # Funding
        print(f"\n=== FUNDING FEES (last 24h) ===")
        try:
            funding = await ex.fapiPrivateGetIncome({'incomeType': 'FUNDING_FEE', 'startTime': since_24h, 'limit': 50})
            total_f = 0.0
            for f in funding:
                ts = datetime.datetime.fromtimestamp(int(f['time'])/1000, tz=datetime.timezone.utc).strftime('%H:%M')
                total_f += float(f['income'])
                print(f"  {ts} {f['symbol']} = {f['income']}")
            print(f"  TOTAL funding: ${total_f:.6f}")
        except Exception as e:
            print(f"  Error: {e}")

        # Commission
        print(f"\n=== COMMISSIONS (last 24h) ===")
        try:
            comm = await ex.fapiPrivateGetIncome({'incomeType': 'COMMISSION', 'startTime': since_24h, 'limit': 50})
            total_c = 0.0
            for c in comm:
                total_c += float(c['income'])
            print(f"  Total commission paid: ${total_c:.6f}")
        except Exception as e:
            print(f"  Error: {e}")

    finally:
        await ex.close()

if __name__ == '__main__':
    asyncio.run(check())
