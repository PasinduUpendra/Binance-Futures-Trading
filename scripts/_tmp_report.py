#!/usr/bin/env python3
"""Temporary script to generate paper trading report."""
import re
import sqlite3
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Balance timeline from orchestrator log ---
balances = {}
log_path = ROOT / "user_data" / "logs" / "orchestrator.log"
with open(log_path) as f:
    for line in f:
        m = re.search(r"^(\d{4}-\d{2}-\d{2}).*Margin balance \(equity\): ([\d.]+)", line)
        if m:
            date, bal = m.group(1), float(m.group(2))
            if date not in balances:
                balances[date] = {"first": bal, "last": bal, "high": bal, "low": bal}
            balances[date]["last"] = bal
            balances[date]["high"] = max(balances[date]["high"], bal)
            balances[date]["low"] = min(balances[date]["low"], bal)

start_bal = 5143.41
print("=" * 80)
print("CLAUDE QUANT — PAPER TRADING REPORT")
print("=" * 80)

print(f"\n{'Date':12s}  {'Open':>10s}  {'High':>10s}  {'Low':>10s}  {'Close':>10s}  {'Day':>7s}  {'Cumul':>7s}")
print("-" * 76)
for d in sorted(balances):
    b = balances[d]
    chg = (b["last"] - b["first"]) / b["first"] * 100
    total = (b["last"] - start_bal) / start_bal * 100
    print(f"{d:12s}  ${b['first']:9.2f}  ${b['high']:9.2f}  ${b['low']:9.2f}  ${b['last']:9.2f}  {chg:+6.2f}%  {total:+6.1f}%")

dates = sorted(balances.keys())
final = balances[dates[-1]]["last"]
peak = max(b["high"] for b in balances.values())
gain = final - start_bal
days = len(dates)
avg_daily = gain / days if days else 0

print(f"\n--- SUMMARY ---")
print(f"Starting balance:  ${start_bal:,.2f}  (Mar 26)")
print(f"Current balance:   ${final:,.2f}  ({dates[-1]})")
print(f"Peak balance:      ${peak:,.2f}")
print(f"Total gain:        ${gain:,.2f}  ({gain/start_bal*100:+.2f}%)")
print(f"Days running:      {days}")
print(f"Avg daily gain:    ${avg_daily:,.2f}  ({avg_daily/start_bal*100:.2f}%/day)")
print(f"Drawdown from peak: {(1 - final/peak)*100:.2f}%")

# --- Trade journal ---
print(f"\n--- TRADE JOURNAL ---")
db_path = ROOT / "user_data" / "agent_state" / "trade_journal.db"
db = sqlite3.connect(str(db_path))
db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM trades ORDER BY timestamp ASC").fetchall()
print(f"Total recorded trades: {len(rows)}")
for r in rows:
    d = dict(r)
    exit_info = f"exit={d['exit_price']}" if d["exit_price"] else "STILL OPEN"
    pnl_info = f"pnl=${d['pnl']}" if d["pnl"] else ""
    print(f"  {d['timestamp'][:16]}  {d['symbol']:20s}  {d['direction']:5s}  entry=${d['entry_price']:>8s}  {exit_info:15s}  {pnl_info}  {d['strategy']}  conf={d['confidence']}  lev={d['leverage']}x")
db.close()

# --- Current positions ---
print(f"\n--- OPEN POSITIONS (from trailing_stops.json) ---")
ts_path = ROOT / "user_data" / "agent_state" / "trailing_stops.json"
if ts_path.exists():
    ts = json.loads(ts_path.read_text())
    for sym, state in ts.items():
        entry = state["entry_price"]
        best = state["best_price"]
        direction = state["direction"]
        activated = state["activated"]
        if direction == "short":
            upnl_pct = (entry - best) / entry * 100
        else:
            upnl_pct = (best - entry) / entry * 100
        print(f"  {sym:20s}  {direction:5s}  entry={entry:>10}  best={best:>12}  trailing={'ACTIVE' if activated else 'inactive'}  move={upnl_pct:+.2f}%")

# --- Error count ---
print(f"\n--- HEALTH ---")
error_count = 0
warn_count = 0
with open(log_path) as f:
    for line in f:
        if " ERROR " in line:
            error_count += 1
        if " WARNING " in line:
            warn_count += 1

# Cycle info
with open(ROOT / "user_data" / "agent_state" / "last_cycle.json") as f:
    cycle = json.load(f)

print(f"Cycles completed:   {cycle['cycle_number']}")
print(f"Last cycle:         {cycle['timestamp']}")
print(f"Circuit breaker:    {cycle['circuit_breaker_level']}")
print(f"Errors in log:      {error_count}")
print(f"Warnings in log:    {warn_count}")

# Drawdown state
with open(ROOT / "user_data" / "agent_state" / "drawdown_state.json") as f:
    dd = json.load(f)
print(f"Peak balance (DD):  ${float(dd['peak_balance']):,.2f}")
print(f"Max drawdown ever:  {dd['max_drawdown_pct']}%")
