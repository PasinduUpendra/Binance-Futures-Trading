"""
Backtest — Aggressive parameter sweep to find maximum realistic returns.

Tests different combinations of:
  - Position sizing (15%, 20%, 25%, 30%)
  - All 9 pairs vs 3 pairs
  - MAX_HOLD_BARS (100, 150, 200)

Uses EXACT same production code as backtest_v4.py.
Reports 21-day, 30-day, 60-day milestones for each config.
"""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from itertools import product

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
from src.risk.volatility_model import VolatilityModel
from src.execution.fee_calculator import FeeCalculator
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.base_strategy import SignalDirection
from src.strategies.cross_asset_consensus import CrossAssetConsensus

DATA_DIR = PROJECT_ROOT / "user_data" / "data"

ALL_PAIRS = [
    "BTC_USDT_USDT", "ETH_USDT_USDT", "SOL_USDT_USDT",
    "DOGE_USDT_USDT", "XRP_USDT_USDT", "LINK_USDT_USDT",
    "AVAX_USDT_USDT", "SUI_USDT_USDT", "ADA_USDT_USDT",
]
THREE_PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]

INITIAL_BALANCE = 68.33
HARD_FLOOR = 30.0
TRAIL_ACTIVATE_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.5

MIN_NOTIONALS = {"BTC": 100.0, "ETH": 20.0}


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def get_min_notional(pair: str) -> float:
    for k, v in MIN_NOTIONALS.items():
        if k in pair:
            return v
    return 5.0


def run_single_backtest(
    pairs: list[str],
    max_position_pct: float,
    max_hold_bars: int,
    data_1h_cache: dict,
    data_4h_cache: dict,
    data_4h_ind_cache: dict,
    ie: IndicatorEngine,
    fee_calc: FeeCalculator,
    vol_model: VolatilityModel,
    adaptive: AdaptiveStrategy,
    consensus: CrossAssetConsensus,
) -> dict:
    """Run a single backtest with given params. Returns summary dict."""

    balance = INITIAL_BALANCE
    peak_balance = balance
    trades = []
    open_positions = []
    daily_pnl = {}
    recent_trade_results = []
    milestones = {}
    milestone_targets = [100, 150, 200, 300, 500, 750, 1000]

    min_1h_len = min(len(data_1h_cache[p]) for p in pairs)
    start_idx = 200

    for i in range(start_idx, min_1h_len):
        if balance < HARD_FLOOR:
            break

        ts = data_1h_cache[pairs[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance}

        # Check milestones
        for mt in milestone_targets:
            if mt not in milestones and balance >= mt:
                days_elapsed = len(daily_pnl)
                milestones[mt] = days_elapsed

        # ─── Manage open positions ───
        closed_idx = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            hi = data_1h_cache[pair]["high"].iloc[i]
            lo = data_1h_cache[pair]["low"].iloc[i]
            cl = data_1h_cache[pair]["close"].iloc[i]
            atr_val = pos["atr"]

            # Supertrend reversal: tighten SL to breakeven
            if pos["strategy"] == "SupertrendTrend":
                current_ts = data_1h_cache[pair]["timestamp"].iloc[i]
                df_4h = data_4h_ind_cache[pair]
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
                    hit_sl, exit_price = True, pos["sl"]
                elif hi >= pos["tp"]:
                    hit_tp, exit_price = True, pos["tp"]
            else:
                if hi >= pos["sl"]:
                    hit_sl, exit_price = True, pos["sl"]
                elif lo <= pos["tp"]:
                    hit_tp, exit_price = True, pos["tp"]

            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and bars_held >= max_hold_bars:
                hit_time, exit_price = True, cl

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
                    "net_pnl": net_pnl,
                    "is_win": net_pnl > 0,
                    "exit_reason": exit_reason,
                    "strategy": pos["strategy"],
                    "margin": pos["margin"],
                })
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

        # Cross-asset consensus
        current_ts = data_1h_cache[pairs[0]]["timestamp"].iloc[i]
        pair_4h_slices = {}
        for p in pairs:
            _df = data_4h_ind_cache[p]
            _valid = _df[_df["timestamp"] <= current_ts]
            if len(_valid) >= 100:
                pair_4h_slices[p] = _valid
        consensus_adj = consensus.compute(pair_4h_slices)

        # Signal generation
        for pair in pairs:
            if pair in open_pairs:
                continue
            if len(open_positions) >= constraints.max_positions:
                break

            current_ts = data_1h_cache[pair]["timestamp"].iloc[i]
            df_4h = data_4h_ind_cache[pair]
            df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
            if len(df_4h_valid) < 100:
                continue

            df_1h_slice = data_1h_cache[pair].iloc[:i + 1].copy()
            df_1h_ind = ie.calculate_all(df_1h_slice.tail(200).copy())

            signal = adaptive.get_signal_multi_tf(df_4h_valid, df_1h_ind)
            if signal is None:
                continue

            # Consensus adjustment
            adj = consensus_adj.get(pair, 0.0)
            if adj != 0.0:
                adjusted_conf = max(0.0, min(100.0, signal.confidence + adj))
                signal = signal.model_copy(update={"confidence": adjusted_conf})

            # Leverage
            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence,
                regime=signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage
            if leverage == 0:
                continue

            # GARCH
            try:
                df_garch = data_1h_cache[pair].iloc[:i + 1].tail(500).copy()
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

            # Position sizing — PARAMETERIZED
            confidence = signal.confidence
            if confidence >= 60:
                position_pct = max_position_pct
            elif confidence >= 45:
                position_pct = max_position_pct * 0.667
            else:
                position_pct = max_position_pct * 0.467

            position_pct *= float(constraints.size_multiplier)
            margin = balance * position_pct
            max_margin = balance * max_position_pct
            if margin > max_margin:
                margin = max_margin
            if margin < 5.0:
                if balance < 5.0:
                    continue
                margin = 5.0

            notional = margin * leverage
            min_notional = get_min_notional(pair)
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

    # Final milestone check
    for mt in milestone_targets:
        if mt not in milestones and balance >= mt:
            milestones[mt] = len(daily_pnl)

    # Compute stats
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["is_win"])
    total_pnl = sum(t["net_pnl"] for t in trades)
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0

    daily_returns = []
    for day_key, dpnl in sorted(daily_pnl.items()):
        if dpnl["start"] > 0:
            daily_returns.append((dpnl["end"] - dpnl["start"]) / dpnl["start"])
    daily_returns = np.array(daily_returns)
    avg_daily = np.mean(daily_returns) * 100 if len(daily_returns) > 0 else 0

    eq = [INITIAL_BALANCE]
    running = INITIAL_BALANCE
    for day_key, dpnl in sorted(daily_pnl.items()):
        eq.append(dpnl["end"])
    peak_arr = np.maximum.accumulate(eq)
    drawdown = (peak_arr - eq) / peak_arr * 100
    max_dd = np.max(drawdown)

    sharpe = 0.0
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(365)

    days = len(daily_pnl)

    # 21-day balance
    day_keys = sorted(daily_pnl.keys())
    bal_21d = daily_pnl[day_keys[20]]["end"] if len(day_keys) > 20 else balance
    bal_30d = daily_pnl[day_keys[29]]["end"] if len(day_keys) > 29 else balance
    bal_60d = daily_pnl[day_keys[59]]["end"] if len(day_keys) > 59 else balance

    return {
        "final_balance": balance,
        "total_return_pct": (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "avg_daily_pct": avg_daily,
        "days": days,
        "trades_per_day": total_trades / days if days > 0 else 0,
        "bal_21d": bal_21d,
        "bal_30d": bal_30d,
        "bal_60d": bal_60d,
        "milestones": milestones,
        "hit_floor": balance < HARD_FLOOR,
    }


def main():
    # Pre-load all data once
    ie = IndicatorEngine()
    fee_calc = FeeCalculator()
    vol_model = VolatilityModel(forecast_horizon=1)
    adaptive = AdaptiveStrategy()
    consensus = CrossAssetConsensus()

    print("Loading data...")
    data_1h = {}
    data_4h = {}
    data_4h_ind = {}
    for pair in ALL_PAIRS:
        data_1h[pair] = load_data(pair, "1h")
        data_4h[pair] = load_data(pair, "4h")
        data_4h_ind[pair] = ie.calculate_all(data_4h[pair].copy())

    # Define parameter grid
    configs = [
        # (label, pairs, max_pos_pct, max_hold_bars)
        ("BASE-3p-15%", THREE_PAIRS, 0.15, 150),
        ("BASE-9p-15%", ALL_PAIRS, 0.15, 150),
        ("AGG1-9p-20%", ALL_PAIRS, 0.20, 150),
        ("AGG2-9p-25%", ALL_PAIRS, 0.25, 150),
        ("AGG3-9p-30%", ALL_PAIRS, 0.30, 150),
        ("AGG4-9p-25%-100bar", ALL_PAIRS, 0.25, 100),
        ("AGG5-9p-25%-200bar", ALL_PAIRS, 0.25, 200),
        ("AGG6-3p-25%", THREE_PAIRS, 0.25, 150),
    ]

    print(f"\nRunning {len(configs)} backtest configurations...")
    print(f"{'='*120}")

    results = []
    for label, pairs, max_pos_pct, max_hold_bars in configs:
        print(f"\n>>> Running: {label} ({len(pairs)} pairs, {max_pos_pct:.0%} sizing, {max_hold_bars} max hold)")
        r = run_single_backtest(
            pairs=pairs,
            max_position_pct=max_pos_pct,
            max_hold_bars=max_hold_bars,
            data_1h_cache=data_1h,
            data_4h_cache=data_4h,
            data_4h_ind_cache=data_4h_ind,
            ie=ie,
            fee_calc=fee_calc,
            vol_model=vol_model,
            adaptive=adaptive,
            consensus=consensus,
        )
        r["label"] = label
        results.append(r)
        print(f"    Final: ${r['final_balance']:.2f} ({r['total_return_pct']:+.1f}%) | "
              f"{r['total_trades']} trades | WR: {r['win_rate']:.1f}% | MaxDD: {r['max_dd']:.1f}% | "
              f"Sharpe: {r['sharpe']:.2f}")

    # Summary table
    print(f"\n\n{'='*140}")
    print("PARAMETER SWEEP RESULTS SUMMARY")
    print(f"{'='*140}")
    header = (f"{'Config':25s} {'Final$':>8s} {'Return':>8s} {'Trades':>6s} {'T/Day':>5s} "
              f"{'WR%':>5s} {'MaxDD':>6s} {'Sharpe':>6s} {'AvgD%':>6s} "
              f"{'21d$':>8s} {'30d$':>8s} {'60d$':>8s} {'$1K day':>7s} {'Floor?':>6s}")
    print(header)
    print("-" * 140)
    for r in results:
        m1k = r["milestones"].get(1000, "N/A")
        floor = "YES" if r["hit_floor"] else "no"
        print(f"{r['label']:25s} ${r['final_balance']:>7.2f} {r['total_return_pct']:>+7.1f}% "
              f"{r['total_trades']:>6d} {r['trades_per_day']:>5.2f} "
              f"{r['win_rate']:>5.1f} {r['max_dd']:>5.1f}% {r['sharpe']:>6.2f} {r['avg_daily_pct']:>5.3f} "
              f"${r['bal_21d']:>7.2f} ${r['bal_30d']:>7.2f} ${r['bal_60d']:>7.2f} "
              f"{str(m1k):>7s} {floor:>6s}")

    # Key question: What's the max 21-day return?
    print(f"\n{'='*80}")
    print("KEY QUESTION: $68 -> $1000 in 21 days feasibility")
    print(f"{'='*80}")
    print(f"Required: 14.6x return in 21 days = 13.5% daily compound")
    for r in results:
        ret_21d = (r["bal_21d"] - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        daily_21d = ((r["bal_21d"] / INITIAL_BALANCE) ** (1/21) - 1) * 100 if r["bal_21d"] > INITIAL_BALANCE else 0
        print(f"  {r['label']:25s}: 21-day balance ${r['bal_21d']:.2f} ({ret_21d:+.1f}%, "
              f"{daily_21d:.2f}%/day compound)")

    # Milestone comparison
    print(f"\n{'='*80}")
    print("MILESTONE COMPARISON (days to reach)")
    print(f"{'='*80}")
    for target in [100, 200, 500, 1000]:
        line = f"  ${target:>5d}: "
        for r in results:
            d = r["milestones"].get(target, "---")
            line += f"  {r['label'][:10]}={str(d):>4s}"
        print(line)


if __name__ == "__main__":
    main()
