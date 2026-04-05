#!/usr/bin/env python3
"""Debug: compare trigger vs regular order fetch on Binance testnet."""
import asyncio, sys, json, os
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/../..")
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from src.execution.order_manager import OrderManager

async def test():
    om = OrderManager()
    await om.connect()
    exchange = om._exchange
    
    # Regular endpoint
    regular = await exchange.fetch_open_orders("LINK/USDT:USDT")
    print(f"Regular endpoint: {len(regular)} orders")
    if regular:
        info = regular[0].get("info", {})
        print(f"  Sample: type={regular[0].get('type')}, origType={info.get('origType')}, info.type={info.get('type')}, stopPrice={regular[0].get('stopPrice')}")
    
    # Trigger endpoint
    trigger = await exchange.fetch_open_orders("LINK/USDT:USDT", params={"trigger": True})
    print(f"Trigger endpoint: {len(trigger)} orders")
    if trigger:
        info = trigger[0].get("info", {})
        print(f"  Sample: type={trigger[0].get('type')}, origType={info.get('origType')}, info.type={info.get('type')}, stopPrice={trigger[0].get('stopPrice')}")
    
    # Check if same order IDs
    reg_ids = {r.get("id") for r in regular}
    trig_ids = {r.get("id") for r in trigger}
    overlap = reg_ids & trig_ids
    only_reg = reg_ids - trig_ids
    only_trig = trig_ids - reg_ids
    print(f"\nOverlap: {len(overlap)}, Only regular: {len(only_reg)}, Only trigger: {len(only_trig)}")
    
    await om.close()

asyncio.run(test())
