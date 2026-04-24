#!/usr/bin/env python3
"""Read-only Binance audit for the last 24 hours.

NO orders placed, modified, or cancelled. Fetches:
  - positions
  - open orders (regular + conditional/trigger)
  - order history (SOL, SUI)
  - user trades/fills (SOL, SUI)
  - income history (realized pnl, funding, commission)
  - account balance snapshot

Uses BINANCE_API_KEY_PROD / BINANCE_API_SECRET_PROD when BINANCE_TESTNET=false.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ccxt.async_support as ccxt_async  # noqa: E402


SYMBOLS = ["SOL/USDT:USDT", "SUI/USDT:USDT"]
FULL_UNIVERSE = [
    "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT",
    "LINK/USDT:USDT", "AVAX/USDT:USDT", "SUI/USDT:USDT", "ADA/USDT:USDT",
]


def _redact(val: str | None) -> str:
    if not val:
        return "<missing>"
    return f"{val[:4]}...{val[-4:]}"


async def main() -> None:
    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    if testnet:
        key = os.getenv("BINANCE_API_KEY")
        sec = os.getenv("BINANCE_API_SECRET")
    else:
        key = os.getenv("BINANCE_API_KEY_PROD")
        sec = os.getenv("BINANCE_API_SECRET_PROD")

    if not key or not sec:
        print("ERROR: API credentials missing")
        return

    print(f"# Binance 24h Audit")
    print(f"testnet_flag={testnet}  api_key={_redact(key)}")

    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=24)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    print(f"window_utc_start={start.isoformat()}")
    print(f"window_utc_end  ={now.isoformat()}")
    print(f"window_local_start={start.astimezone().isoformat()}")
    print(f"window_local_end  ={now.astimezone().isoformat()}")
    print()

    exchange = ccxt_async.binanceusdm({
        "apiKey": key,
        "secret": sec,
        "enableRateLimit": True,
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
    })
    if testnet:
        exchange.enable_demo_trading(True)

    try:
        # Balance snapshot
        bal = await exchange.fetch_balance()
        usdt = bal.get("USDT", {})
        print("## Balance snapshot (now)")
        print(f"  total={usdt.get('total')} free={usdt.get('free')} used={usdt.get('used')}")
        print()

        # Positions
        print("## Open positions (now)")
        positions = await exchange.fetch_positions()
        open_pos = [p for p in positions if float(p.get("contracts") or 0) > 0]
        print(f"  open_count={len(open_pos)}")
        for p in open_pos:
            print(f"  {p['symbol']} side={p['side']} contracts={p['contracts']} "
                  f"entry={p.get('entryPrice')} uPnL={p.get('unrealizedPnl')} lev={p.get('leverage')}")
        print()

        # Open orders (full universe + conditional)
        print("## Open orders (now) — regular + conditional/trigger, full universe")
        for sym in FULL_UNIVERSE:
            reg = await exchange.fetch_open_orders(sym)
            trig = await exchange.fetch_open_orders(sym, params={"trigger": True})
            if reg or trig:
                print(f"  {sym}: regular={len(reg)} trigger={len(trig)}")
                for o in reg + trig:
                    print(f"    type={o.get('type')} side={o['side']} qty={o.get('amount')} "
                          f"stopPrice={o.get('stopPrice')} reduceOnly={o['info'].get('reduceOnly')} "
                          f"status={o['status']} ts={o.get('datetime')}")
        print()

        # Order history (24h) for SOL + SUI
        print("## Order history last 24h — SOL and SUI")
        for sym in SYMBOLS:
            try:
                orders = await exchange.fetch_orders(sym, since=start_ms, limit=100)
            except Exception as e:
                print(f"  {sym}: fetch_orders ERROR {type(e).__name__}: {e}")
                orders = []
            print(f"  {sym}: total_orders_in_window={len(orders)}")
            for o in orders:
                print(f"    {o.get('datetime')} type={o.get('type')} side={o['side']} "
                      f"qty={o.get('amount')} price={o.get('price')} status={o['status']} "
                      f"reduceOnly={o['info'].get('reduceOnly')} clientOID={o.get('clientOrderId')}")
        print()

        # User trades/fills (24h) for SOL + SUI
        print("## User fills last 24h — SOL and SUI")
        total_fills = 0
        for sym in SYMBOLS:
            try:
                trades = await exchange.fetch_my_trades(sym, since=start_ms, limit=200)
            except Exception as e:
                print(f"  {sym}: fetch_my_trades ERROR {type(e).__name__}: {e}")
                trades = []
            print(f"  {sym}: fills_in_window={len(trades)}")
            total_fills += len(trades)
            for t in trades:
                print(f"    {t['datetime']} side={t['side']} qty={t['amount']} "
                      f"price={t['price']} cost={t['cost']} fee={t['fee']} maker={t.get('takerOrMaker')}")
        print(f"  TOTAL fills across SOL+SUI in 24h: {total_fills}")
        print()

        # Income history (24h) — funding, commission, realized pnl
        print("## Income history last 24h (all symbols)")
        try:
            inc = await exchange.fapiPrivateGetIncome({
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            })
        except Exception as e:
            print(f"  fapiPrivateGetIncome ERROR {type(e).__name__}: {e}")
            inc = []

        agg: dict[str, float] = {}
        by_symbol_type: dict[tuple[str, str], float] = {}
        for row in inc:
            t = row.get("incomeType", "?")
            amt = float(row.get("income") or 0)
            sym = row.get("symbol") or "-"
            agg[t] = agg.get(t, 0) + amt
            by_symbol_type[(sym, t)] = by_symbol_type.get((sym, t), 0) + amt
        print(f"  total_income_rows={len(inc)}")
        for t, amt in sorted(agg.items()):
            print(f"    {t}: {amt:+.6f} USDT")
        if by_symbol_type:
            print("  by_symbol_type:")
            for (s, t), amt in sorted(by_symbol_type.items()):
                print(f"    {s} {t}: {amt:+.6f}")
        print()

        # Explicitly show funding for SOL + SUI only
        print("## Funding paid/received last 24h — SOL and SUI only")
        for sym in SYMBOLS:
            sym_raw = sym.replace("/USDT:USDT", "USDT")
            f = [r for r in inc if r.get("symbol") == sym_raw and r.get("incomeType") == "FUNDING_FEE"]
            total = sum(float(r.get("income") or 0) for r in f)
            print(f"  {sym_raw}: count={len(f)} total={total:+.6f} USDT")

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
