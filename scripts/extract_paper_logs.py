#!/usr/bin/env python3
"""Extract all paper trading logs for review."""
import sqlite3
import re

print("=" * 70)
print("PAPER TRADING LOG — Claude Quant Bot")
print("=" * 70)

# 1. Trade Journal
db1 = sqlite3.connect("user_data/agent_state/trade_journal.db")
db1.row_factory = sqlite3.Row
trades = db1.execute("SELECT * FROM trades ORDER BY timestamp").fetchall()
print(f"\n### TRADE JOURNAL: {len(trades)} trades\n")
for i, r in enumerate(trades, 1):
    d = dict(r)
    print(f"Trade #{i}: {d['symbol']} {d['direction'].upper()} via {d['strategy']}")
    print(f"  Time:       {d['timestamp']}")
    print(f"  Entry:      {d['entry_price']} | Size: {d['size']} | Leverage: {d['leverage']}x")
    print(f"  Confidence: {d['confidence']}% | SL: {d['stop_loss']} | TP: {d['take_profit']}")
    print(f"  Exit:       {d['exit_price']} | PnL: {d['pnl']}")
    print()
db1.close()

# 2. Consolidated DB
db2 = sqlite3.connect("user_data/claude_quant.db")
db2.row_factory = sqlite3.Row

reports = db2.execute("SELECT * FROM daily_reports ORDER BY report_date").fetchall()
print(f"### DAILY REPORTS: {len(reports)} days\n")
for r in reports:
    d = dict(r)
    print(f"  {d['report_date']}: ${d['start_balance']} -> ${d['end_balance']} | net_pnl=${d['net_pnl']} ({d['pnl_pct']}%) | trades={d['trades_count']} W={d['wins']}/L={d['losses']}")

stats = db2.execute(
    "SELECT COUNT(*) as c, MIN(timestamp) as t1, MAX(timestamp) as t2 FROM cycle_history"
).fetchone()
print(f"\n### CYCLE HISTORY: {stats['c']} total cycles")
print(f"  Period: {stats['t1']} to {stats['t2']}")

tc = db2.execute(
    "SELECT * FROM cycle_history WHERE trade_placed=1 ORDER BY timestamp"
).fetchall()
print(f"\n### CYCLES WITH TRADES: {len(tc)}")
for r in tc:
    d = dict(r)
    print(f"  Cycle #{d['cycle_number']} at {d['timestamp']}: bal=${d['balance']} details={d['trade_details']}")

sc = db2.execute(
    "SELECT COUNT(*) as c FROM cycle_history WHERE signal_generated=1 AND trade_placed=0"
).fetchone()
nc = db2.execute(
    "SELECT COUNT(*) as c FROM cycle_history WHERE signal_generated=0"
).fetchone()
print(f"\n### SIGNAL STATS")
print(f"  Signals generated but NOT traded: {sc['c']}")
print(f"  No signal cycles: {nc['c']}")
db2.close()

# 3. Key log entries
print(f"\n### KEY BOT.LOG ENTRIES (filtered)\n")
patterns = re.compile(
    r"(Starting Claude|Initial balance|Cycle \d+ (starting|complete)"
    r"|circuit.breaker (GREEN|YELLOW|RED|DEAD)"
    r"|HALT|DEAD"
    r"|place_market|MARKET_ORDER|ORDER_PLACED"
    r"|place_stop_loss|place_take_profit|STOP_MARKET|TAKE_PROFIT"
    r"|FILL|fill.*price"
    r"|daily.*report.*saved|Daily report"
    r"|Best signal|No.*valid.*signal|signal.*generated"
    r"|ERROR|CRITICAL"
    r"|Supertrend.*flip|reversal.*exit"
    r"|trailing.*stop.*activate|trailing.*close"
    r"|4H.*close.*handler"
    r"|hourly_cycle.*trade|4h_candle.*trade"
    r"|position.*count)",
    re.I,
)
try:
    with open("user_data/logs/bot.log") as f:
        for line in f:
            if patterns.search(line):
                print(f"  {line.rstrip()}")
except FileNotFoundError:
    print("  (bot.log not found)")

# 4. Older log
print(f"\n### OLDER LOG (pre-fix, key entries)\n")
try:
    with open("user_data/logs/orchestrator.log.2026-03-24-pre-fix") as f:
        for line in f:
            if patterns.search(line):
                print(f"  {line.rstrip()}")
except FileNotFoundError:
    print("  (orchestrator.log.2026-03-24-pre-fix not found)")

# 5. State files
print(f"\n### CURRENT STATE FILES\n")
import json
for path in [
    "user_data/agent_state/daily_state.json",
    "user_data/agent_state/drawdown_state.json",
    "user_data/agent_state/last_cycle.json",
    "user_data/agent_state/watchdog_state.json",
]:
    try:
        with open(path) as f:
            data = json.load(f)
        print(f"--- {path} ---")
        print(json.dumps(data, indent=2))
        print()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"--- {path}: {e} ---\n")
