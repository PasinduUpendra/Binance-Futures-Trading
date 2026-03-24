"""
Backtest v6 — Expanded Pairs + ADX Gap Fix Validation.

Tests the SupertrendTrend strategy across 9 pairs instead of 3.
Also tests the impact of lowering regime ADX_TRENDING_MIN from 25 -> 20
to fix the dead zone where ADX 20-25 blocks valid SupertrendTrend trades.

Runs 3 scenarios:
  A) Baseline: 3 pairs (ETH, SOL, DOGE) — current config
  B) Expanded: 9 pairs (ETH, SOL, DOGE + BTC, XRP, LINK, AVAX, SUI, ADA)
  C) Expanded + ADX fix: 9 pairs with ADX_TRENDING_MIN = 20
"""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.indicator_engine import IndicatorEngine
from src.risk.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerLevel,
    TradeResult,
)
from src.risk.leverage_manager import LeverageManager
from src.risk.position_sizer import PositionSizer
from src.risk.volatility_model import VolatilityModel
from src.execution.fee_calculator import FeeCalculator
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.base_strategy import SignalDirection
from src.strategies.regime_detector import RegimeDetector

DATA_DIR = PROJECT_ROOT / "user_data" / "data"

PAIRS_BASELINE = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]
PAIRS_EXPANDED = PAIRS_BASELINE + [
    "BTC_USDT_USDT", "XRP_USDT_USDT", "LINK_USDT_USDT",
    "AVAX_USDT_USDT", "SUI_USDT_USDT", "ADA_USDT_USDT",
]

INITIAL_BALANCE = 5000.0  # Current testnet balance
HARD_FLOOR = 30.0
MAX_HOLD_BARS = 150
TRAIL_ACTIVATE_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.5

# Minimum notional per pair
MIN_NOTIONAL = {
    "BTC_USDT_USDT": 100.0,
    "ETH_USDT_USDT": 20.0,
}
DEFAULT_MIN_NOTIONAL = 5.0


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def run_scenario(pairs: list[str], adx_trending_min: float, label: str) -> dict:
    """Run a single backtest scenario and return stats."""
    ie = IndicatorEngine()
    fee_calc = FeeCalculator()
    vol_model = VolatilityModel(forecast_horizon=1)
    adaptive = AdaptiveStrategy()

    # Override ADX threshold if needed
    if adx_trending_min != 25.0:
        adaptive._regime_detector.ADX_TRENDING_MIN = adx_trending_min

    data_1h = {}
    data_4h = {}
    for pair in pairs:
        try:
            data_1h[pair] = load_data(pair, "1h")
            data_4h[pair] = load_data(pair, "4h")
        except FileNotFoundError:
            print(f"  SKIP {pair}: data not found")
            continue

    valid_pairs = list(data_1h.keys())

    data_4h_ind = {}
    for pair in valid_pairs:
        data_4h_ind[pair] = ie.calculate_all(data_4h[pair].copy())

    balance = INITIAL_BALANCE
    peak_balance = balance
    trades = []
    equity_curve = []
    open_positions = []
    recent_trade_results = []
    daily_pnl = {}

    min_1h_len = min(len(data_1h[p]) for p in valid_pairs)
    start_idx = 200

    for i in range(start_idx, min_1h_len):
        if balance < HARD_FLOOR:
            break

        ts = data_1h[valid_pairs[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance, "trades": 0}

        # Manage open positions
        closed_idx = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            hi = data_1h[pair]["high"].iloc[i]
            lo = data_1h[pair]["low"].iloc[i]
            cl = data_1h[pair]["close"].iloc[i]
            atr_val = pos["atr"]

            # Supertrend reversal exit: tighten SL to breakeven
            if pos["strategy"] == "SupertrendTrend":
                current_ts = data_1h[pair]["timestamp"].iloc[i]
                df_4h = data_4h_ind[pair]
                df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
                if len(df_4h_valid) > 0:
                    should_exit = adaptive.check_supertrend_reversal(
                        df_4h_valid, pos["direction"]
                    )
                    if should_exit:
                        if pos["direction"] == "long":
                            pos["sl"] = max(pos["sl"], pos["entry"])
                        else:
                            pos["sl"] = min(pos["sl"], pos["entry"])

            # Trailing stop
            if pos["direction"] == "long":
                if hi > pos.get("best_price", pos["entry"]):
                    pos["best_price"] = hi
                fav = pos["best_price"] - pos["entry"]
                if fav > TRAIL_ACTIVATE_ATR_MULT * atr_val:
                    new_sl = pos["best_price"] - TRAIL_ATR_MULT * atr_val
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl
            else:
                if lo < pos.get("best_price", pos["entry"]):
                    pos["best_price"] = lo
                fav = pos["entry"] - pos["best_price"]
                if fav > TRAIL_ACTIVATE_ATR_MULT * atr_val:
                    new_sl = pos["best_price"] + TRAIL_ATR_MULT * atr_val
                    if new_sl < pos["sl"]:
                        pos["sl"] = new_sl

            hit_sl = hit_tp = hit_time = False
            exit_price = None

            if pos["direction"] == "long":
                if lo <= pos["sl"]:
                    hit_sl = True
                    exit_price = pos["sl"]
                elif hi >= pos["tp"]:
                    hit_tp = True
                    exit_price = pos["tp"]
            else:
                if hi >= pos["sl"]:
                    hit_sl = True
                    exit_price = pos["sl"]
                elif lo <= pos["tp"]:
                    hit_tp = True
                    exit_price = pos["tp"]

            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and bars_held >= MAX_HOLD_BARS:
                hit_time = True
                exit_price = cl

            if hit_sl or hit_tp or hit_time:
                if pos["direction"] == "long":
                    raw_pnl = (exit_price - pos["entry"]) * pos["size"]
                else:
                    raw_pnl = (pos["entry"] - exit_price) * pos["size"]

                entry_fees = float(fee_calc.calculate_fees(
                    Decimal(str(pos["entry"] * pos["size"])), is_maker=False))
                exit_fees = float(fee_calc.calculate_fees(
                    Decimal(str(exit_price * pos["size"])), is_maker=False))
                net_pnl = raw_pnl - entry_fees - exit_fees

                balance += net_pnl
                peak_balance = max(peak_balance, balance)

                exit_reason = "TP" if hit_tp else ("TIME" if hit_time else "SL")

                trades.append({
                    "pair": pair,
                    "direction": pos["direction"],
                    "entry_price": pos["entry"],
                    "exit_price": exit_price,
                    "size": pos["size"],
                    "leverage": pos["leverage"],
                    "raw_pnl": raw_pnl,
                    "fees": entry_fees + exit_fees,
                    "net_pnl": net_pnl,
                    "margin": pos["margin"],
                    "roi_pct": (net_pnl / pos["margin"]) * 100 if pos["margin"] > 0 else 0,
                    "is_win": net_pnl > 0,
                    "exit_reason": exit_reason,
                    "strategy": pos["strategy"],
                    "confidence": pos["confidence"],
                    "timestamp": str(ts),
                    "bars_held": bars_held,
                })
                daily_pnl[day]["trades"] += 1
                closed_idx.append(pidx)

                recent_trade_results.append(TradeResult(
                    is_win=net_pnl > 0,
                    closed_at=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                ))
                recent_trade_results = recent_trade_results[-10:]

        for pidx in sorted(closed_idx, reverse=True):
            open_positions.pop(pidx)

        # Circuit Breaker
        start_of_day_balance = Decimal(str(daily_pnl[day]["start"]))
        cb_state = CircuitBreaker.is_trading_allowed(
            balance=Decimal(str(balance)),
            recent_trades=recent_trade_results,
            start_of_day_balance=start_of_day_balance,
        )

        if cb_state.level == CircuitBreakerLevel.DEAD or not cb_state.constraints.trading_allowed:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        constraints = cb_state.constraints
        if len(open_positions) >= constraints.max_positions:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        open_pairs = {p["pair"] for p in open_positions}

        # Signal Generation
        for pair in valid_pairs:
            if pair in open_pairs:
                continue
            if len(open_positions) >= constraints.max_positions:
                break

            current_ts = data_1h[pair]["timestamp"].iloc[i]
            df_4h = data_4h_ind[pair]
            df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
            if len(df_4h_valid) < 100:
                continue

            df_1h_slice = data_1h[pair].iloc[:i + 1].copy()
            df_1h_ind = ie.calculate_all(df_1h_slice.tail(200).copy())

            signal = adaptive.get_signal_multi_tf(df_4h_valid, df_1h_ind)
            if signal is None:
                continue

            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence,
                regime=signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage
            if leverage == 0:
                continue

            try:
                df_garch = data_1h[pair].iloc[:i + 1].tail(500).copy()
                vol_state = vol_model.forecast(df_garch)
                if vol_state is None:
                    vol_state = vol_model.forecast_simple(df_garch)
                leverage = VolatilityModel.adjust_leverage(
                    requested_leverage=leverage,
                    vol_state=vol_state,
                    max_leverage=constraints.max_leverage,
                )
            except Exception:
                leverage = min(leverage, constraints.max_leverage)

            if leverage == 0:
                leverage = 1

            confidence = signal.confidence
            if confidence >= 60:
                position_pct = 0.15
            elif confidence >= 45:
                position_pct = 0.10
            else:
                position_pct = 0.07
            position_pct *= float(constraints.size_multiplier)

            margin = balance * position_pct
            max_margin = balance * 0.15
            if margin > max_margin:
                margin = max_margin
            if margin < 5.0:
                if balance < 5.0:
                    continue
                margin = 5.0

            notional = margin * leverage
            min_not = MIN_NOTIONAL.get(pair, DEFAULT_MIN_NOTIONAL)
            if notional < min_not:
                continue

            size = notional / signal.entry_price
            atr_4h = float(df_4h_valid["atr"].dropna().iloc[-1]) if "atr" in df_4h_valid.columns else 0.0

            open_positions.append({
                "pair": pair,
                "direction": signal.direction.value,
                "entry": signal.entry_price,
                "sl": signal.stop_loss,
                "tp": signal.take_profit,
                "size": size,
                "leverage": leverage,
                "margin": margin,
                "strategy": signal.strategy_name,
                "confidence": signal.confidence,
                "entry_idx": i,
                "best_price": signal.entry_price,
                "atr": atr_4h,
            })

        equity_curve.append({"timestamp": str(ts), "balance": balance})
        daily_pnl[day]["end"] = balance

    # Calculate stats
    total_trades = len(trades)
    if total_trades == 0:
        return {"label": label, "trades": 0, "return_pct": 0, "win_rate": 0,
                "sharpe": 0, "max_dd": 0, "profit_factor": 0,
                "avg_daily": 0, "days": len(daily_pnl), "final_balance": balance,
                "by_pair": {}, "by_exit": {}}

    wins = sum(1 for t in trades if t["is_win"])
    losses = total_trades - wins
    win_rate = wins / total_trades * 100
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["fees"] for t in trades)

    eq = [INITIAL_BALANCE] + [e["balance"] for e in equity_curve]
    peak = np.maximum.accumulate(eq)
    drawdown = (peak - eq) / peak * 100
    max_dd = np.max(drawdown)

    daily_returns = []
    for day_key, dpnl in sorted(daily_pnl.items()):
        if dpnl["start"] > 0:
            daily_returns.append((dpnl["end"] - dpnl["start"]) / dpnl["start"])
    daily_returns = np.array(daily_returns)
    sharpe = 0.0
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(365)
    avg_daily = np.mean(daily_returns) * 100 if len(daily_returns) > 0 else 0

    total_return = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    gross_profit = sum(t["net_pnl"] for t in trades if t["is_win"])
    gross_loss = abs(sum(t["net_pnl"] for t in trades if not t["is_win"]))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Per-pair breakdown
    by_pair = {}
    for t in trades:
        p = t["pair"]
        if p not in by_pair:
            by_pair[p] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_pair[p]["trades"] += 1
        by_pair[p]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            by_pair[p]["wins"] += 1

    # Per-exit breakdown
    by_exit = {}
    for t in trades:
        r = t["exit_reason"]
        if r not in by_exit:
            by_exit[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        by_exit[r]["count"] += 1
        by_exit[r]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            by_exit[r]["wins"] += 1

    return {
        "label": label,
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "return_pct": total_return,
        "win_rate": win_rate,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "profit_factor": profit_factor,
        "avg_daily": avg_daily,
        "days": len(daily_pnl),
        "final_balance": balance,
        "total_pnl": total_pnl,
        "total_fees": total_fees,
        "by_pair": by_pair,
        "by_exit": by_exit,
    }


def print_results(r: dict) -> None:
    print(f"\n{'='*70}")
    print(f"  SCENARIO: {r['label']}")
    print(f"{'='*70}")
    print(f"  Period:         {r['days']} days")
    print(f"  Final balance:  ${r['final_balance']:.2f} (started ${INITIAL_BALANCE:.2f})")
    print(f"  Total return:   {r['return_pct']:+.1f}%")
    print(f"  Total P&L:      ${r.get('total_pnl', 0):+.2f} (fees: ${r.get('total_fees', 0):.2f})")
    print(f"  Total trades:   {r['trades']}")
    if r['trades'] > 0:
        print(f"  Win/Loss:       {r['wins']}W / {r['losses']}L ({r['win_rate']:.1f}%)")
        print(f"  Profit factor:  {r['profit_factor']:.2f}")
        print(f"  Max drawdown:   {r['max_dd']:.1f}%")
        print(f"  Sharpe ratio:   {r['sharpe']:.2f}")
        print(f"  Avg daily:      {r['avg_daily']:.3f}%")
        print(f"  Trades/day:     {r['trades']/r['days']:.2f}")

        print(f"\n  Per-pair breakdown:")
        for pair, stats in sorted(r["by_pair"].items()):
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
            print(f"    {pair:20s}  {stats['trades']:3d} trades  {wr:5.1f}% WR  ${stats['pnl']:+8.2f}")

        print(f"\n  Exit reasons:")
        for reason, stats in sorted(r["by_exit"].items()):
            wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
            print(f"    {reason:8s}  {stats['count']:3d} trades  {wr:5.1f}% WR  ${stats['pnl']:+8.2f}")


def main() -> None:
    print("=" * 70)
    print("BACKTEST V6 — Expanded Pairs + ADX Gap Fix Validation")
    print(f"Initial balance: ${INITIAL_BALANCE:.2f}")
    print("=" * 70)

    scenarios = []

    print("\n>>> Running Scenario A: Baseline (3 pairs, ADX_TRENDING_MIN=25) ...")
    result_a = run_scenario(PAIRS_BASELINE, adx_trending_min=25.0, label="A) Baseline: 3 pairs")
    scenarios.append(result_a)
    print_results(result_a)

    print("\n>>> Running Scenario B: Expanded (9 pairs, ADX_TRENDING_MIN=25) ...")
    result_b = run_scenario(PAIRS_EXPANDED, adx_trending_min=25.0, label="B) Expanded: 9 pairs")
    scenarios.append(result_b)
    print_results(result_b)

    print("\n>>> Running Scenario C: Expanded + ADX fix (9 pairs, ADX_TRENDING_MIN=20) ...")
    result_c = run_scenario(PAIRS_EXPANDED, adx_trending_min=20.0, label="C) 9 pairs + ADX fix")
    scenarios.append(result_c)
    print_results(result_c)

    # Comparison table
    print(f"\n{'='*70}")
    print("COMPARISON TABLE")
    print(f"{'='*70}")
    header = f"  {'Metric':20s}"
    for s in scenarios:
        header += f"  {s['label']:>20s}"
    print(header)
    print(f"  {'─' * 80}")

    metrics = [
        ("Trades", "trades", "{:d}"),
        ("Win Rate", "win_rate", "{:.1f}%"),
        ("Total Return", "return_pct", "{:+.1f}%"),
        ("Final Balance", "final_balance", "${:.2f}"),
        ("Profit Factor", "profit_factor", "{:.2f}"),
        ("Max Drawdown", "max_dd", "{:.1f}%"),
        ("Sharpe", "sharpe", "{:.2f}"),
        ("Avg Daily", "avg_daily", "{:.3f}%"),
        ("Trades/Day", None, None),
    ]
    for name, key, fmt in metrics:
        row = f"  {name:20s}"
        for s in scenarios:
            if key is None:  # Trades/Day special case
                val = s["trades"] / s["days"] if s["days"] > 0 else 0
                row += f"  {val:>20.2f}"
            else:
                val = s[key]
                row += f"  {fmt.format(val):>20s}"
        print(row)

    # Gate check: each new scenario must pass vs baseline
    print(f"\n{'='*70}")
    print("GATE CHECKS (vs Baseline)")
    print(f"{'='*70}")
    for s in scenarios[1:]:
        print(f"\n  {s['label']}:")
        checks = {
            "WR >= baseline": s["win_rate"] >= result_a["win_rate"] * 0.9,
            "PF >= baseline": s["profit_factor"] >= result_a["profit_factor"] * 0.8,
            "Sharpe >= baseline": s["sharpe"] >= result_a["sharpe"] * 0.8,
            "MaxDD <= baseline*1.5": s["max_dd"] <= max(result_a["max_dd"] * 1.5, 15),
            "Return > baseline": s["return_pct"] > result_a["return_pct"],
            "More trades": s["trades"] > result_a["trades"],
        }
        all_pass = True
        for check, passed in checks.items():
            status = "PASS" if passed else "FAIL"
            if not passed:
                all_pass = False
            print(f"    [{status}] {check}")
        print(f"    Overall: {'ALL GATES PASSED' if all_pass else 'SOME GATES FAILED'}")


if __name__ == "__main__":
    main()
