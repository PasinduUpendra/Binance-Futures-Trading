"""Deep dive: why 501/548 cycles have empty regime and why signals don't become trades."""
import sqlite3
import json

c = sqlite3.connect("user_data/claude_quant.db")
c.row_factory = sqlite3.Row

# 1. Sample of cycles with empty regime
print("=== EMPTY REGIME CYCLES (sample of 10) ===")
rows = c.execute(
    "SELECT cycle_number, timestamp, circuit_breaker_level, balance, regime, signal_generated, trade_placed, errors, trade_details, duration_seconds "
    "FROM cycle_history WHERE regime='' ORDER BY timestamp DESC LIMIT 10"
).fetchall()
for r in rows:
    d = dict(r)
    errs = d["errors"] or "none"
    print(f"  #{d['cycle_number']} {d['timestamp'][:19]}: cb={d['circuit_breaker_level']} bal={d['balance']} sig={d['signal_generated']} trade={d['trade_placed']} dur={d['duration_seconds']}s err_len={len(errs)}")
    if errs != "none" and errs != "[]":
        print(f"    errors: {errs[:200]}")

# 2. ALL cycles where trade_placed=1
print("\n=== ALL CYCLES WHERE TRADE WAS PLACED ===")
rows = c.execute(
    "SELECT cycle_number, timestamp, circuit_breaker_level, balance, regime, trade_details "
    "FROM cycle_history WHERE trade_placed=1 ORDER BY timestamp"
).fetchall()
for r in rows:
    d = dict(r)
    print(f"  #{d['cycle_number']} {d['timestamp'][:19]}: cb={d['circuit_breaker_level']} bal={d['balance']} regime={d['regime']}")
    print(f"    details: {d['trade_details'][:200] if d['trade_details'] else 'none'}")

# 3. Signal cycles where trade was NOT placed — full details
print("\n=== SIGNAL WITHOUT TRADE (last 20) ===")
rows = c.execute(
    "SELECT cycle_number, timestamp, balance, regime, trade_details, errors, positions_closed "
    "FROM cycle_history WHERE signal_generated=1 AND trade_placed=0 ORDER BY timestamp DESC LIMIT 20"
).fetchall()
for r in rows:
    d = dict(r)
    print(f"  #{d['cycle_number']} {d['timestamp'][:19]}: bal={d['balance']} regime={d['regime']} closed={d['positions_closed']}")
    print(f"    details: {d['trade_details'][:200] if d['trade_details'] else 'none'}")
    print(f"    errors: {(d['errors'] or 'none')[:200]}")

# 4. Error analysis
print("\n=== ERRORS DISTRIBUTION ===")
rows = c.execute("SELECT errors FROM cycle_history WHERE errors IS NOT NULL AND errors != '' AND errors != '[]'").fetchall()
print(f"Cycles with non-empty errors: {len(rows)}")
error_types = {}
for r in rows:
    err = r["errors"][:100]
    error_types[err] = error_types.get(err, 0) + 1
for err, cnt in sorted(error_types.items(), key=lambda x: -x[1])[:10]:
    print(f"  ({cnt}x) {err}")

# 5. positions_closed analysis
print("\n=== POSITIONS CLOSED ===")
rows = c.execute("SELECT timestamp, positions_closed FROM cycle_history WHERE positions_closed IS NOT NULL AND positions_closed != '' AND positions_closed != '0' ORDER BY timestamp").fetchall()
print(f"Cycles that closed positions: {len(rows)}")
for r in rows:
    print(f"  {r['timestamp'][:19]}: {r['positions_closed'][:200] if r['positions_closed'] else 'none'}")

# 6. Balance trajectory
print("\n=== BALANCE TRAJECTORY (daily) ===")
rows = c.execute(
    "SELECT DATE(timestamp) as day, MIN(balance) as min_bal, MAX(balance) as max_bal, AVG(balance) as avg_bal, COUNT(*) as cycles "
    "FROM cycle_history GROUP BY day ORDER BY day"
).fetchall()
for r in rows:
    print(f"  {r['day']}: min={r['min_bal']:.2f} max={r['max_bal']:.2f} avg={r['avg_bal']:.2f} cycles={r['cycles']}")

c.close()
