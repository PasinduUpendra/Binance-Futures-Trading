#!/usr/bin/env python3
"""Debug script to inspect raw order fields from Binance."""
import asyncio, sys, json, os
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/../..")
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from src.execution.order_manager import OrderManager

async def test():
    om = OrderManager()
    await om.connect()
    exchange = om._exchange
    raw = await exchange.fetch_open_orders("LINK/USDT:USDT", params={"trigger": True})
    print(f"Total trigger orders for LINK: {len(raw)}")
    
    # Get unique origType values
    orig_types = set()
    for r in raw:
        info = r.get("info", {})
        orig_types.add(info.get("origType", "NONE"))
    print(f"Unique origType values: {orig_types}")
    
    # Print 2 samples with different stopPrice
    prices_seen = set()
    for r in raw:
        sp = r.get("stopPrice")
        if sp not in prices_seen:
            prices_seen.add(sp)
            info = r.get("info", {})
            out = {
                "type": r.get("type"),
                "side": r.get("side"),
                "stopPrice": sp,
                "origType": info.get("origType"),
                "info_type": info.get("type"),
                "closePosition": info.get("closePosition"),
                "reduceOnly": info.get("reduceOnly"),
            }
            print(json.dumps(out))
            if len(prices_seen) >= 3:
                break
    
    # Also check ADA
    raw_ada = await exchange.fetch_open_orders("ADA/USDT:USDT", params={"trigger": True})
    print(f"\nTotal trigger orders for ADA: {len(raw_ada)}")
    prices_seen2 = set()
    for r in raw_ada:
        sp = r.get("stopPrice")
        if sp not in prices_seen2:
            prices_seen2.add(sp)
            info = r.get("info", {})
            print(json.dumps({
                "origType": info.get("origType"),
                "side": r.get("side"),
                "stopPrice": sp,
            }))
            if len(prices_seen2) >= 3:
                break
    
    await om.close()

asyncio.run(test())
