"""Fetch 15-day market data for all 9 pairs and analyze vs bot trades."""
import asyncio
import sys
sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone
import ccxt.async_support as ccxt_async
import json
import sqlite3

PAIRS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "DOGE/USDT:USDT", "XRP/USDT:USDT", "LINK/USDT:USDT",
    "AVAX/USDT:USDT", "SUI/USDT:USDT", "ADA/USDT:USDT"
]

async def main():
    exchange = ccxt_async.binanceusdm({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    since = int((datetime.now(timezone.utc) - timedelta(days=15)).timestamp() * 1000)

    print("=== 15-DAY MARKET DATA (4H candles) ===")
    for pair in PAIRS:
        try:
            candles = await exchange.fetch_ohlcv(pair, "4h", since=since, limit=500)
            if not candles:
                print(f"\n{pair}: NO DATA")
                continue

            opens = [c[1] for c in candles]
            highs = [c[2] for c in candles]
            lows = [c[3] for c in candles]
            closes = [c[4] for c in candles]
            volumes = [c[5] for c in candles]

            first_close = closes[0]
            last_close = closes[-1]
            pct_change = ((last_close - first_close) / first_close) * 100
            high_of_period = max(highs)
            low_of_period = min(lows)
            max_range_pct = ((high_of_period - low_of_period) / low_of_period) * 100

            # Find best long opportunity (buy low, sell high)
            best_long_entry = min(range(len(lows)), key=lambda i: lows[i])
            best_long_exit_candidates = [i for i in range(best_long_entry, len(highs))]
            if best_long_exit_candidates:
                best_long_exit = max(best_long_exit_candidates, key=lambda i: highs[i])
                best_long_pct = ((highs[best_long_exit] - lows[best_long_entry]) / lows[best_long_entry]) * 100
            else:
                best_long_pct = 0

            # Find best short opportunity (sell high, buy low)
            best_short_entry = max(range(len(highs)), key=lambda i: highs[i])
            best_short_exit_candidates = [i for i in range(best_short_entry, len(lows))]
            if best_short_exit_candidates:
                best_short_exit = min(best_short_exit_candidates, key=lambda i: lows[i])
                best_short_pct = ((highs[best_short_entry] - lows[best_short_exit]) / highs[best_short_entry]) * 100
            else:
                best_short_pct = 0

            # Count significant swings (>2% moves)
            swings = 0
            for i in range(1, len(closes)):
                move = abs((closes[i] - closes[i-1]) / closes[i-1]) * 100
                if move > 2:
                    swings += 1

            # Average 4H candle range
            avg_candle_range = sum((highs[i] - lows[i]) / lows[i] * 100 for i in range(len(candles))) / len(candles)

            ts_first = datetime.fromtimestamp(candles[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            ts_last = datetime.fromtimestamp(candles[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

            print(f"\n{pair} ({len(candles)} candles, {ts_first} to {ts_last}):")
            print(f"  Period return: {pct_change:+.2f}%")
            print(f"  Range: {low_of_period:.4f} — {high_of_period:.4f} ({max_range_pct:.1f}% range)")
            print(f"  Best theoretical long: {best_long_pct:.1f}% (at {lows[best_long_entry]:.4f} → {highs[best_long_exit]:.4f})")
            print(f"  Best theoretical short: {best_short_pct:.1f}% (at {highs[best_short_entry]:.4f} → {lows[best_short_exit]:.4f})")
            print(f"  Significant swings (>2%): {swings}")
            print(f"  Avg 4H candle range: {avg_candle_range:.2f}%")
            print(f"  First/Last close: {first_close:.4f} / {last_close:.4f}")

        except Exception as e:
            print(f"\n{pair}: ERROR - {e}")

    # Now fetch current positions
    print("\n\n=== CURRENT EXCHANGE STATE ===")
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        
        exchange2 = ccxt_async.binanceusdm({
            "apiKey": os.getenv("BINANCE_API_KEY"),
            "secret": os.getenv("BINANCE_API_SECRET"),
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        exchange2.enable_demo_trading(True)
        
        positions = await exchange2.fetch_positions()
        open_pos = [p for p in positions if abs(float(p.get("contracts", 0))) > 0]
        print(f"Open positions: {len(open_pos)}")
        for p in open_pos:
            print(f"  {p['symbol']} {p['side']} size={p['contracts']} entry={p['entryPrice']} "
                  f"uPnL={p.get('unrealizedPnl', 'N/A')} liq={p.get('liquidationPrice', 'N/A')}")
        
        balance = await exchange2.fetch_balance()
        total = float(balance.get("total", {}).get("USDT", 0))
        free = float(balance.get("free", {}).get("USDT", 0))
        print(f"Balance: total={total:.2f} free={free:.2f}")
        
        # Count open orders
        for pair in PAIRS:
            try:
                orders = await exchange2.fetch_open_orders(pair, params={"trigger": True})
                if orders:
                    print(f"  {pair}: {len(orders)} conditional orders")
            except Exception:
                pass
        
        await exchange2.close()
    except Exception as e:
        print(f"Error fetching exchange state: {e}")

    await exchange.close()

    # Also check why signals don't become trades
    print("\n\n=== WHY SIGNALS DON'T BECOME TRADES ===")
    c = sqlite3.connect("user_data/claude_quant.db")
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT errors FROM cycle_history WHERE signal_generated=1 AND trade_placed=0 ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()
    for i, r in enumerate(rows):
        errs = r["errors"] or "none"
        print(f"  Signal #{i+1}: {errs[:300]}")
    c.close()

asyncio.run(main())
