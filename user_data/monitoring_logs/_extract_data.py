"""Extract all bot data for market comparison analysis."""
import sqlite3
import json

# 1. Daily reports
c = sqlite3.connect("user_data/claude_quant.db")
c.row_factory = sqlite3.Row

print("=== DAILY REPORTS ===")
rows = c.execute("SELECT * FROM daily_reports ORDER BY report_date DESC").fetchall()
for r in rows:
    d = dict(r)
    print(f"{d['report_date']}: start={d['start_balance']} end={d['end_balance']} pnl%={d['pnl_pct']} trades={d['trades_count']} w={d['wins']} l={d['losses']}")

print("\n=== TRAILING STOPS ===")
rows = c.execute("SELECT * FROM trailing_stops").fetchall()
for r in rows:
    print(dict(r))

print("\n=== CYCLES WITH SIGNALS (last 30) ===")
rows = c.execute(
    "SELECT * FROM cycle_history WHERE signal_generated=1 OR trade_placed=1 ORDER BY timestamp DESC LIMIT 30"
).fetchall()
for r in rows:
    d = dict(r)
    det = (d["trade_details"] or "")[:150]
    print(f"#{d['cycle_number']} {d['timestamp']}: cb={d['circuit_breaker_level']} bal={d['balance']} regime={d['regime']} sig={d['signal_generated']} trade={d['trade_placed']} det={det}")

print("\n=== CYCLE STATS ===")
total = c.execute("SELECT COUNT(*) FROM cycle_history").fetchone()[0]
sig = c.execute("SELECT COUNT(*) FROM cycle_history WHERE signal_generated=1").fetchone()[0]
traded = c.execute("SELECT COUNT(*) FROM cycle_history WHERE trade_placed=1").fetchone()[0]
err = c.execute("SELECT COUNT(*) FROM cycle_history WHERE errors IS NOT NULL AND errors != ''").fetchone()[0]
first = c.execute("SELECT MIN(timestamp) FROM cycle_history").fetchone()[0]
last = c.execute("SELECT MAX(timestamp) FROM cycle_history").fetchone()[0]
print(f"Total cycles: {total}, Signals: {sig}, Trades: {traded}, Errors: {err}")
print(f"Period: {first} to {last}")

# Regime distribution
print("\n=== REGIME DISTRIBUTION ===")
rows = c.execute(
    "SELECT regime, COUNT(*) as cnt FROM cycle_history GROUP BY regime ORDER BY cnt DESC"
).fetchall()
for r in rows:
    print(f"  {r[0] or 'empty'}: {r[1]}")

c.close()

# 2. Trade journal
print("\n=== TRADE JOURNAL (all trades) ===")
c2 = sqlite3.connect("user_data/agent_state/trade_journal.db")
c2.row_factory = sqlite3.Row
rows = c2.execute("SELECT * FROM trades ORDER BY timestamp DESC").fetchall()
for r in rows:
    d = dict(r)
    print(f"{d['timestamp'][:19]} {d['symbol']} {d['direction']} entry={d['entry_price']} exit={d['exit_price']} pnl={d['pnl']} strat={d['strategy']} conf={d['confidence']} lev={d['leverage']} lessons={d['lessons']}")
c2.close()
