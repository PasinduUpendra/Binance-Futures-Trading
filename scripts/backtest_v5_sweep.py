"""
Backtest v5 — Parameter Sweep for Maximum Compounding.

PURPOSE: Sweep Supertrend parameters, MAX_HOLD_BARS, and ST_REV exit modes
across the SAME production code paths used by backtest_v4.py to find the
parameter combination that maximises risk-adjusted compound returns.

Sweep axes:
  1. Supertrend period:     [7, 8, 9, 10, 12, 14]
  2. Supertrend multiplier: [2.0, 2.5, 3.0, 3.5]
  3. MAX_HOLD_BARS:         [90, 120, 150, 180, 240]
  4. ST_REV exit mode:      ["immediate", "tighten_to_breakeven", "ignore"]

Uses same production classes as v4:
  - AdaptiveStrategy.get_signal_multi_tf()
  - PositionSizer, LeverageManager, VolatilityModel, CircuitBreaker
  - FeeCalculator (Binance VIP 0)
  - IndicatorEngine.calculate_supertrend() with custom params

Output: sorted results table + JSON to user_data/backtest_results/
"""

import itertools
import json
import logging
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

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

# Suppress ALL logging output during the sweep (must be AFTER imports)
logging.disable(logging.CRITICAL)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.root.addHandler(logging.NullHandler())

DATA_DIR = PROJECT_ROOT / "user_data" / "data"
PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]
INITIAL_BALANCE = 68.33
HARD_FLOOR = 30.0

# Trailing stop params (constant across sweep — matching production)
TRAIL_ACTIVATE_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.5

# Sweep grid
SUPERTREND_PERIODS = [7, 8, 9, 10, 12, 14]
SUPERTREND_MULTIPLIERS = [2.0, 2.5, 3.0, 3.5]
MAX_HOLD_BARS_OPTIONS = [90, 120, 150, 180, 240]
ST_REV_MODES = ["immediate", "tighten_to_breakeven"]


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def run_single_backtest(
    *,
    data_1h: dict[str, pd.DataFrame],
    data_1h_ind: dict[str, pd.DataFrame],
    data_4h: dict[str, pd.DataFrame],
    ie: IndicatorEngine,
    fee_calc: FeeCalculator,
    vol_model: VolatilityModel,
    adaptive: AdaptiveStrategy,
    st_period: int,
    st_multiplier: float,
    max_hold_bars: int,
    st_rev_mode: str,
) -> dict[str, Any]:
    """Run one backtest with specific parameters. Returns summary dict."""
    balance = INITIAL_BALANCE
    peak_balance = balance
    trades: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    daily_pnl: dict[str, dict[str, Any]] = {}
    recent_trade_results: list[TradeResult] = []

    # Pre-compute 4H indicators with CUSTOM Supertrend params
    data_4h_ind: dict[str, pd.DataFrame] = {}
    # Pre-compute mapping: for each 1H bar index → last valid 4H bar index
    h1_to_h4_idx: dict[str, np.ndarray] = {}
    for pair in PAIRS:
        df = data_4h[pair].copy()
        df = ie.calculate_all(df)
        df = ie.calculate_supertrend(df, period=st_period, multiplier=st_multiplier)
        data_4h_ind[pair] = df

        # Build index mapping: for each 1H timestamp, find the last 4H bar <= that ts
        ts_1h = data_1h[pair]["timestamp"].values
        ts_4h = df["timestamp"].values
        h1_to_h4_idx[pair] = np.searchsorted(ts_4h, ts_1h, side="right") - 1

    min_1h_len = min(len(data_1h[p]) for p in PAIRS)
    start_idx = 200  # Skip warmup

    for i in range(start_idx, min_1h_len):
        if balance < HARD_FLOOR:
            break

        ts = data_1h[PAIRS[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance, "trades": 0}

        # ─── Manage open positions ───
        closed_idx: list[int] = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            hi = data_1h[pair]["high"].iloc[i]
            lo = data_1h[pair]["low"].iloc[i]
            cl = data_1h[pair]["close"].iloc[i]
            atr_val = pos["atr"]

            # ─── Supertrend reversal check ───
            st_reversal_detected = False
            if pos["strategy"] == "SupertrendTrend":
                h4_idx = h1_to_h4_idx[pair][i]
                if h4_idx >= 100:
                    df_4h_valid = data_4h_ind[pair].iloc[:h4_idx + 1]
                    should_exit = adaptive.check_supertrend_reversal(
                        df_4h_valid, pos["direction"]
                    )
                    if should_exit:
                        st_reversal_detected = True

            # ─── Handle ST_REV based on mode ───
            st_exit = False
            if st_reversal_detected:
                if st_rev_mode == "immediate":
                    st_exit = True
                elif st_rev_mode == "tighten_to_breakeven":
                    # Move SL to entry price (breakeven) instead of closing
                    if pos["direction"] == "long":
                        pos["sl"] = max(pos["sl"], pos["entry"])
                    else:
                        pos["sl"] = min(pos["sl"], pos["entry"])
                    # st_exit stays False — let SL/TP/TIME handle it
                elif st_rev_mode == "ignore":
                    pass  # Completely ignore — let other exits handle it

            # ─── Trailing stop logic ───
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

            hit_sl = hit_tp = hit_time = hit_st = False
            exit_price = None

            if st_exit:
                hit_st = True
                exit_price = cl

            if not hit_st:
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

            # Time-based exit
            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and not hit_st and bars_held >= max_hold_bars:
                hit_time = True
                exit_price = cl

            if hit_sl or hit_tp or hit_time or hit_st:
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

                if hit_st:
                    exit_reason = "ST_REV"
                elif hit_tp:
                    exit_reason = "TP"
                elif hit_time:
                    exit_reason = "TIME"
                else:
                    exit_reason = "SL"

                trade_record = {
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
                }
                trades.append(trade_record)
                daily_pnl[day]["trades"] += 1
                closed_idx.append(pidx)

                recent_trade_results.append(TradeResult(
                    is_win=net_pnl > 0,
                    closed_at=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                ))
                recent_trade_results = recent_trade_results[-10:]

        for pidx in sorted(closed_idx, reverse=True):
            open_positions.pop(pidx)

        # ─── Circuit Breaker ───
        start_of_day_balance = Decimal(str(daily_pnl[day]["start"]))
        cb_state = CircuitBreaker.is_trading_allowed(
            balance=Decimal(str(balance)),
            recent_trades=recent_trade_results,
            start_of_day_balance=start_of_day_balance,
        )

        if cb_state.level == CircuitBreakerLevel.DEAD:
            daily_pnl[day]["end"] = balance
            continue

        if not cb_state.constraints.trading_allowed:
            daily_pnl[day]["end"] = balance
            continue

        constraints = cb_state.constraints
        if len(open_positions) >= constraints.max_positions:
            daily_pnl[day]["end"] = balance
            continue

        open_pairs = {p["pair"] for p in open_positions}

        # ─── Signal Generation ───
        for pair in PAIRS:
            if pair in open_pairs:
                continue
            if len(open_positions) >= constraints.max_positions:
                break

            h4_idx = h1_to_h4_idx[pair][i]
            if h4_idx < 100:
                continue
            df_4h_valid = data_4h_ind[pair].iloc[:h4_idx + 1]

            df_1h_ind_slice = data_1h_ind[pair].iloc[max(0, i - 199):i + 1]

            # Use AdaptiveStrategy — it reads the 4H data which already has
            # overwritten supertrend columns from the sweep params
            signal = adaptive.get_signal_multi_tf(df_4h_valid, df_1h_ind_slice)

            if signal is None:
                continue

            # ─── Leverage ───
            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence,
                regime=signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage
            if leverage == 0:
                continue

            # ─── GARCH ───
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

            # ─── Position Sizing (confidence-based) ───
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

            min_notional = 20.0 if "ETH" in pair else 5.0
            if notional < min_notional:
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

        daily_pnl[day]["end"] = balance

    # ─── Compute metrics ───
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "st_period": st_period,
            "st_multiplier": st_multiplier,
            "max_hold_bars": max_hold_bars,
            "st_rev_mode": st_rev_mode,
            "trades": 0,
            "final_balance": balance,
            "total_return_pct": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "avg_daily_pct": 0.0,
            "exit_breakdown": {},
        }

    wins = sum(1 for t in trades if t["is_win"])
    losses = total_trades - wins
    win_rate = wins / total_trades * 100
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["fees"] for t in trades)

    eq = [INITIAL_BALANCE] + [balance]
    # Build better equity curve from daily data
    daily_returns = []
    for day_key, dpnl in sorted(daily_pnl.items()):
        if dpnl["start"] > 0:
            daily_returns.append((dpnl["end"] - dpnl["start"]) / dpnl["start"])
    daily_returns_arr = np.array(daily_returns)

    eq_series = [INITIAL_BALANCE]
    running = INITIAL_BALANCE
    for r in daily_returns:
        running *= (1 + r)
        eq_series.append(running)
    peak = np.maximum.accumulate(eq_series)
    drawdown = (peak - eq_series) / np.where(peak > 0, peak, 1) * 100
    max_dd = float(np.max(drawdown))

    sharpe = 0.0
    if len(daily_returns_arr) > 1 and np.std(daily_returns_arr) > 0:
        sharpe = float(np.mean(daily_returns_arr) / np.std(daily_returns_arr) * np.sqrt(365))
    avg_daily = float(np.mean(daily_returns_arr) * 100) if len(daily_returns_arr) > 0 else 0.0

    total_return = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    gross_wins = sum(t["net_pnl"] for t in trades if t["is_win"])
    gross_losses = abs(sum(t["net_pnl"] for t in trades if not t["is_win"]))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Exit reason breakdown
    exit_breakdown: dict[str, dict[str, Any]] = {}
    for t in trades:
        r = t["exit_reason"]
        if r not in exit_breakdown:
            exit_breakdown[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        exit_breakdown[r]["count"] += 1
        exit_breakdown[r]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            exit_breakdown[r]["wins"] += 1

    # Per-pair breakdown
    pair_breakdown: dict[str, dict[str, Any]] = {}
    for t in trades:
        p = t["pair"].replace("_USDT_USDT", "")
        if p not in pair_breakdown:
            pair_breakdown[p] = {"count": 0, "pnl": 0.0, "wins": 0}
        pair_breakdown[p]["count"] += 1
        pair_breakdown[p]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            pair_breakdown[p]["wins"] += 1

    return {
        "st_period": st_period,
        "st_multiplier": st_multiplier,
        "max_hold_bars": max_hold_bars,
        "st_rev_mode": st_rev_mode,
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "final_balance": round(balance, 2),
        "total_return_pct": round(total_return, 2),
        "win_rate": round(win_rate, 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 1),
        "profit_factor": round(profit_factor, 2),
        "avg_daily_pct": round(avg_daily, 4),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "exit_breakdown": exit_breakdown,
        "pair_breakdown": pair_breakdown,
    }


def run_sweep() -> None:
    """Run the full parameter sweep."""
    print("=" * 80)
    print("BACKTEST V5 — PARAMETER SWEEP")
    print("=" * 80)
    print(f"Supertrend periods:     {SUPERTREND_PERIODS}")
    print(f"Supertrend multipliers: {SUPERTREND_MULTIPLIERS}")
    print(f"MAX_HOLD_BARS:          {MAX_HOLD_BARS_OPTIONS}")
    print(f"ST_REV modes:           {ST_REV_MODES}")

    total_combos = (len(SUPERTREND_PERIODS) * len(SUPERTREND_MULTIPLIERS)
                    * len(MAX_HOLD_BARS_OPTIONS) * len(ST_REV_MODES))
    print(f"Total combinations:     {total_combos}")
    print("=" * 80)

    # Load data once
    ie = IndicatorEngine()
    fee_calc = FeeCalculator()
    vol_model = VolatilityModel(forecast_horizon=1)
    adaptive = AdaptiveStrategy()

    data_1h: dict[str, pd.DataFrame] = {}
    data_4h: dict[str, pd.DataFrame] = {}
    data_1h_ind: dict[str, pd.DataFrame] = {}
    for pair in PAIRS:
        data_1h[pair] = load_data(pair, "1h")
        data_4h[pair] = load_data(pair, "4h")
    print("Data loaded.")

    # Pre-compute 1H indicators ONCE per pair (constant across sweep combos)
    for pair in PAIRS:
        data_1h_ind[pair] = ie.calculate_all(data_1h[pair].copy())
    print("1H indicators pre-computed.\n")

    results: list[dict[str, Any]] = []
    start_time = time.time()
    quiet = "--quiet" in sys.argv

    for idx, (period, mult, hold_bars, rev_mode) in enumerate(
        itertools.product(
            SUPERTREND_PERIODS, SUPERTREND_MULTIPLIERS,
            MAX_HOLD_BARS_OPTIONS, ST_REV_MODES
        ),
        start=1,
    ):
        result = run_single_backtest(
            data_1h=data_1h,
            data_1h_ind=data_1h_ind,
            data_4h=data_4h,
            ie=ie,
            fee_calc=fee_calc,
            vol_model=vol_model,
            adaptive=adaptive,
            st_period=period,
            st_multiplier=mult,
            max_hold_bars=hold_bars,
            st_rev_mode=rev_mode,
        )
        results.append(result)
        if not quiet:
            print(
                f"[{idx}/{total_combos}] ST({period},{mult}) "
                f"hold={hold_bars} rev={rev_mode:22s} "
                f"→ {result['trades']:3d} trades  "
                f"WR={result['win_rate']:5.1f}%  "
                f"PnL=${result.get('total_pnl', 0):+8.2f}  "
                f"Sharpe={result['sharpe']:5.2f}  "
                f"DD={result['max_drawdown']:4.1f}%  "
                f"PF={result['profit_factor']:5.2f}"
            )
        elif idx % 30 == 0 or idx == total_combos:
            elapsed = time.time() - start_time
            print(f"  Progress: {idx}/{total_combos} ({elapsed:.0f}s)", flush=True)

    total_elapsed = time.time() - start_time

    # ─── Sort by Sharpe ratio (risk-adjusted) ───
    results_sorted = sorted(results, key=lambda r: r["sharpe"], reverse=True)

    # ─── Print top 20 results ───
    print(f"\n{'=' * 120}")
    print(f"TOP 20 PARAMETER COMBINATIONS (sorted by Sharpe ratio)")
    print(f"{'=' * 120}")
    header = (
        f"{'Rank':>4}  {'ST(P,M)':>10}  {'Hold':>5}  {'STRev':>22}  "
        f"{'Trades':>6}  {'WR%':>6}  {'PnL$':>9}  {'Return%':>8}  "
        f"{'Sharpe':>7}  {'PF':>6}  {'MaxDD%':>7}  {'AvgD%':>7}"
    )
    print(header)
    print("-" * 120)

    for rank, r in enumerate(results_sorted[:20], start=1):
        print(
            f"{rank:>4}  "
            f"ST({r['st_period']:2d},{r['st_multiplier']:.1f})  "
            f"{r['max_hold_bars']:>5}  "
            f"{r['st_rev_mode']:>22}  "
            f"{r['trades']:>6}  "
            f"{r['win_rate']:>5.1f}%  "
            f"${r.get('total_pnl', 0):>+8.2f}  "
            f"{r['total_return_pct']:>+7.1f}%  "
            f"{r['sharpe']:>6.2f}  "
            f"{r['profit_factor']:>5.2f}  "
            f"{r['max_drawdown']:>6.1f}%  "
            f"{r['avg_daily_pct']:>6.3f}%"
        )

    # ─── Print baseline comparison ───
    baseline = [r for r in results if r["st_period"] == 10 and r["st_multiplier"] == 3.0
                and r["max_hold_bars"] == 120 and r["st_rev_mode"] == "immediate"]
    if baseline:
        bl = baseline[0]
        print(f"\n{'=' * 120}")
        print("BASELINE: ST(10, 3.0) / hold=120 / rev=immediate")
        print(f"{'=' * 120}")
        print(
            f"  Trades: {bl['trades']}  WR: {bl['win_rate']:.1f}%  "
            f"PnL: ${bl.get('total_pnl', 0):+.2f}  Return: {bl['total_return_pct']:+.1f}%  "
            f"Sharpe: {bl['sharpe']:.2f}  PF: {bl['profit_factor']:.2f}  "
            f"MaxDD: {bl['max_drawdown']:.1f}%"
        )
        if bl.get("exit_breakdown"):
            print("  Exit breakdown:")
            for reason, data in sorted(bl["exit_breakdown"].items()):
                wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
                print(f"    {reason:8s}: {data['count']:3d} trades  {wr:5.1f}% WR  ${data['pnl']:+.2f}")

    # ─── Comparison: ST_REV modes with baseline ST params ───
    print(f"\n{'=' * 120}")
    print("ST_REV MODE COMPARISON (ST=10,3.0 / hold=120)")
    print(f"{'=' * 120}")
    for mode in ST_REV_MODES:
        matches = [r for r in results if r["st_period"] == 10 and r["st_multiplier"] == 3.0
                   and r["max_hold_bars"] == 120 and r["st_rev_mode"] == mode]
        if matches:
            m = matches[0]
            print(
                f"  {mode:22s}  {m['trades']:3d} trades  WR={m['win_rate']:5.1f}%  "
                f"PnL=${m.get('total_pnl', 0):+8.2f}  Sharpe={m['sharpe']:5.2f}  "
                f"PF={m['profit_factor']:5.2f}  DD={m['max_drawdown']:4.1f}%"
            )

    # ─── Comparison: MAX_HOLD_BARS with baseline ST params ───
    print(f"\n{'=' * 120}")
    print("MAX_HOLD_BARS COMPARISON (ST=10,3.0 / rev=immediate)")
    print(f"{'=' * 120}")
    for hold in MAX_HOLD_BARS_OPTIONS:
        matches = [r for r in results if r["st_period"] == 10 and r["st_multiplier"] == 3.0
                   and r["max_hold_bars"] == hold and r["st_rev_mode"] == "immediate"]
        if matches:
            m = matches[0]
            print(
                f"  hold={hold:4d}  {m['trades']:3d} trades  WR={m['win_rate']:5.1f}%  "
                f"PnL=${m.get('total_pnl', 0):+8.2f}  Sharpe={m['sharpe']:5.2f}  "
                f"PF={m['profit_factor']:5.2f}  DD={m['max_drawdown']:4.1f}%"
            )
            if m.get("exit_breakdown") and "TIME" in m["exit_breakdown"]:
                te = m["exit_breakdown"]["TIME"]
                twr = te["wins"] / te["count"] * 100 if te["count"] > 0 else 0
                print(f"           TIME exits: {te['count']} trades, {twr:.0f}% WR, ${te['pnl']:+.2f}")

    # ─── Best overall winner ───
    winner = results_sorted[0]
    print(f"\n{'=' * 120}")
    print("WINNER")
    print(f"{'=' * 120}")
    print(f"  Parameters: ST({winner['st_period']}, {winner['st_multiplier']}) "
          f"/ hold={winner['max_hold_bars']} / rev={winner['st_rev_mode']}")
    print(f"  Trades: {winner['trades']}  WR: {winner['win_rate']:.1f}%  "
          f"PnL: ${winner.get('total_pnl', 0):+.2f}  Return: {winner['total_return_pct']:+.1f}%  "
          f"Sharpe: {winner['sharpe']:.2f}  PF: {winner['profit_factor']:.2f}  "
          f"MaxDD: {winner['max_drawdown']:.1f}%  AvgDaily: {winner['avg_daily_pct']:.4f}%")
    if winner.get("exit_breakdown"):
        print("  Exit breakdown:")
        for reason, data in sorted(winner["exit_breakdown"].items()):
            wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
            print(f"    {reason:8s}: {data['count']:3d} trades  {wr:5.1f}% WR  ${data['pnl']:+.2f}")
    if winner.get("pair_breakdown"):
        print("  Pair breakdown:")
        for pair, data in sorted(winner["pair_breakdown"].items()):
            wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
            print(f"    {pair:8s}: {data['count']:3d} trades  {wr:5.1f}% WR  ${data['pnl']:+.2f}")

    # ─── Gate check ───
    print(f"\n{'=' * 120}")
    print("GATE CHECK — Winner vs Baseline")
    print(f"{'=' * 120}")
    if baseline:
        bl = baseline[0]
        checks = []
        checks.append(("Sharpe > baseline (3.98)",
                       winner["sharpe"] > bl["sharpe"],
                       f"{winner['sharpe']:.2f} vs {bl['sharpe']:.2f}"))
        checks.append(("PF > baseline (5.39)",
                       winner["profit_factor"] > bl["profit_factor"],
                       f"{winner['profit_factor']:.2f} vs {bl['profit_factor']:.2f}"))
        checks.append(("MaxDD < 15%",
                       winner["max_drawdown"] < 15.0,
                       f"{winner['max_drawdown']:.1f}%"))
        checks.append(("Trade count increases",
                       winner["trades"] > bl["trades"],
                       f"{winner['trades']} vs {bl['trades']}"))
        checks.append(("WR >= 55%",
                       winner["win_rate"] >= 55.0,
                       f"{winner['win_rate']:.1f}%"))
        checks.append(("Return > baseline",
                       winner["total_return_pct"] > bl["total_return_pct"],
                       f"{winner['total_return_pct']:+.1f}% vs {bl['total_return_pct']:+.1f}%"))

        all_passed = True
        for check_name, passed, detail in checks:
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"  {status}  {check_name}: {detail}")
            if not passed:
                all_passed = False

        if all_passed:
            print("\n  ✅ ALL GATE CHECKS PASSED — Winner is safe to deploy")
        else:
            print("\n  ⚠️  SOME GATE CHECKS FAILED — Review before deploying")
            print("     Consider: Sharpe or PF regression may be acceptable if")
            print("     trade count and total return both significantly improve.")

    # ─── Save all results ───
    output_path = PROJECT_ROOT / "user_data" / "backtest_results" / "parameter_sweep_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({
        "sweep_timestamp": datetime.now(timezone.utc).isoformat(),
        "grid": {
            "supertrend_periods": SUPERTREND_PERIODS,
            "supertrend_multipliers": SUPERTREND_MULTIPLIERS,
            "max_hold_bars": MAX_HOLD_BARS_OPTIONS,
            "st_rev_modes": ST_REV_MODES,
        },
        "baseline": baseline[0] if baseline else None,
        "winner": winner,
        "all_results": results_sorted,
        "elapsed_seconds": round(total_elapsed, 1),
    }, indent=2, default=str))
    print(f"\nFull results saved to {output_path}")
    print(f"Sweep completed in {total_elapsed:.0f}s")


if __name__ == "__main__":
    run_sweep()
