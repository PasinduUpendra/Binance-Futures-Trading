"""Compare Sprint 1 backtest vs baseline."""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
baseline = json.load(open(ROOT / "user_data/backtest_results/backtest_v4_baseline_pre_sprint1.json"))
sprint1 = json.load(open(ROOT / "user_data/backtest_results/backtest_v4_results.json"))

print("SPRINT 1 vs BASELINE COMPARISON")
print("=" * 65)
fmt = "{:22s} {:>14s} {:>14s} {:>12s}"
print(fmt.format("Metric", "Baseline", "Sprint 1", "Delta"))
print("-" * 65)

rows = [
    ("Final balance", baseline["final_balance"], sprint1["final_balance"], "$", ".2f"),
    ("Total return %", baseline["total_return_pct"], sprint1["total_return_pct"], "", ".1f"),
    ("Total trades", baseline["total_trades"], sprint1["total_trades"], "", "d"),
    ("Win rate %", baseline["win_rate"], sprint1["win_rate"], "", ".1f"),
    ("Sharpe ratio", baseline["sharpe"], sprint1["sharpe"], "", ".2f"),
    ("Max drawdown %", baseline["max_drawdown"], sprint1["max_drawdown"], "", ".2f"),
    ("Profit factor", baseline["profit_factor"], sprint1["profit_factor"], "", ".2f"),
    ("Avg daily %", baseline["avg_daily_pct"], sprint1["avg_daily_pct"], "", ".3f"),
    ("Total fees", baseline["total_fees"], sprint1["total_fees"], "$", ".2f"),
]

for label, bv, sv, prefix, f in rows:
    delta = sv - bv
    if f == "d":
        print(fmt.format(label, f"{prefix}{bv:{f}}", f"{prefix}{sv:{f}}", f"{delta:+d}"))
    else:
        print(fmt.format(label, f"{prefix}{bv:{f}}", f"{prefix}{sv:{f}}", f"{delta:+{f}}"))

# Strategy signal counts
print("\nSTRATEGY SIGNAL COUNTS:")
all_strats = set(list(baseline["signal_stats"]["by_strategy"].keys()) +
                 list(sprint1["signal_stats"]["by_strategy"].keys()))
for strat in sorted(all_strats):
    b = baseline["signal_stats"]["by_strategy"].get(strat, 0)
    s = sprint1["signal_stats"]["by_strategy"].get(strat, 0)
    print(f"  {strat:20s}: {b:3d} -> {s:3d} (delta: {s - b:+d})")

# DoD Gate Checks
print("\nDOD GATE CHECKS:")
return_pct_change = (sprint1["total_return_pct"] - baseline["total_return_pct"]) / baseline["total_return_pct"] * 100
dd_delta = sprint1["max_drawdown"] - baseline["max_drawdown"]
pf_ratio = sprint1["profit_factor"] / baseline["profit_factor"]

checks = [
    ("Return within +/-10%", f"{return_pct_change:+.1f}%", abs(return_pct_change) <= 10),
    ("Max DD within +2pp", f"{dd_delta:+.2f}pp", dd_delta <= 2.0),
    ("PF >= baseline x 0.9", f"{pf_ratio:.3f}", pf_ratio >= 0.9),
    ("Sharpe >= baseline x 0.9", f"{sprint1['sharpe']/baseline['sharpe']:.3f}", sprint1["sharpe"]/baseline["sharpe"] >= 0.9),
    ("Win rate improved", f"{sprint1['win_rate']-baseline['win_rate']:+.1f}pp", sprint1["win_rate"] >= baseline["win_rate"]),
]

for label, value, passed in checks:
    status = "PASS" if passed else "FAIL"
    print(f"  {label:28s}  {value:>10s}  [{status}]")

# Per-strategy P&L comparison
print("\nPER-STRATEGY BREAKDOWN (Sprint 1):")
strat_data = {}
for t in sprint1["trades"]:
    s = t["strategy"]
    if s not in strat_data:
        strat_data[s] = {"count": 0, "wins": 0, "pnl": 0.0}
    strat_data[s]["count"] += 1
    strat_data[s]["pnl"] += t["net_pnl"]
    if t["is_win"]:
        strat_data[s]["wins"] += 1

for s, d in sorted(strat_data.items()):
    wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
    print(f"  {s:20s}: {d['count']:3d} trades, {wr:.1f}% WR, ${d['pnl']:+.2f}")

print("\nPER-STRATEGY BREAKDOWN (Baseline):")
strat_data_b = {}
for t in baseline["trades"]:
    s = t["strategy"]
    if s not in strat_data_b:
        strat_data_b[s] = {"count": 0, "wins": 0, "pnl": 0.0}
    strat_data_b[s]["count"] += 1
    strat_data_b[s]["pnl"] += t["net_pnl"]
    if t["is_win"]:
        strat_data_b[s]["wins"] += 1

for s, d in sorted(strat_data_b.items()):
    wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
    print(f"  {s:20s}: {d['count']:3d} trades, {wr:.1f}% WR, ${d['pnl']:+.2f}")
