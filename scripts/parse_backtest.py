"""Parse backtest_v4 results JSON and print summary."""
import json
import numpy as np
from pathlib import Path

results_path = Path(__file__).parent.parent / "user_data" / "backtest_results" / "backtest_v4_results.json"
with open(results_path) as f:
    r = json.load(f)

trades = r["trades"]
eq = r["equity_curve"]

total = len(trades)
wins = sum(1 for t in trades if t["is_win"])
losses = total - wins
wr = wins / total * 100 if total else 0
pnl = sum(t["net_pnl"] for t in trades)
fees = sum(t["fees"] for t in trades)

balances = [68.33] + [e["balance"] for e in eq]
peak = np.maximum.accumulate(balances)
dd = (peak - np.array(balances)) / peak * 100
max_dd = float(np.max(dd))
final = balances[-1]
ret = (final - 68.33) / 68.33 * 100

daily_bal: dict[str, float] = {}
for e in eq:
    d = e["timestamp"][:10]
    daily_bal[d] = e["balance"]
days = len(daily_bal)

prev = 68.33
daily_returns = []
for d in sorted(daily_bal.keys()):
    b = daily_bal[d]
    if prev > 0:
        daily_returns.append((b - prev) / prev)
    prev = b
dr = np.array(daily_returns)
sharpe = float(np.mean(dr) / np.std(dr) * np.sqrt(365)) if len(dr) > 1 and np.std(dr) > 0 else 0.0

avg_win = float(np.mean([t["net_pnl"] for t in trades if t["is_win"]])) if wins else 0
avg_loss = float(np.mean([abs(t["net_pnl"]) for t in trades if not t["is_win"]])) if losses else 0
trades_per_day = total / days if days else 0

print("=" * 50)
print("v6.17 BACKTEST RESULTS")
print("=" * 50)
print(f"Period:           {days} days")
print(f"Final balance:    ${final:.2f}")
print(f"Total return:     {ret:+.1f}%")
print(f"Total P&L:        ${pnl:+.2f}")
print(f"Total fees:       ${fees:.2f}")
print(f"Trades:           {total}")
print(f"Trades/day:       {trades_per_day:.2f}")
print(f"Win/Loss:         {wins}W / {losses}L ({wr:.1f}% WR)")
print(f"Avg win:          ${avg_win:+.2f}")
print(f"Avg loss:         ${avg_loss:.2f}")
print(f"Max drawdown:     {max_dd:.1f}%")
print(f"Sharpe:           {sharpe:.2f}")
print(f"Avg daily return: {np.mean(dr)*100:.3f}%")
print()

print("STRATEGY BREAKDOWN:")
strats: dict[str, dict] = {}
for t in trades:
    s = t["strategy"]
    if s not in strats:
        strats[s] = {"n": 0, "w": 0, "pnl": 0.0}
    strats[s]["n"] += 1
    strats[s]["pnl"] += t["net_pnl"]
    if t["is_win"]:
        strats[s]["w"] += 1
for s, d in strats.items():
    print(f"  {s}: {d['n']} trades, {d['w']/d['n']*100:.1f}% WR, ${d['pnl']:+.2f}")

print()
print("EXIT REASONS:")
exits: dict[str, dict] = {}
for t in trades:
    reason = t["exit_reason"]
    if reason not in exits:
        exits[reason] = {"n": 0, "pnl": 0.0}
    exits[reason]["n"] += 1
    exits[reason]["pnl"] += t["net_pnl"]
for reason, d in exits.items():
    print(f"  {reason}: {d['n']} trades, ${d['pnl']:+.2f}")
