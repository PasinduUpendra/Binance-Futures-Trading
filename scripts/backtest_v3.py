"""
Backtest v3 — Simplified 4H Supertrend + MR hybrid.

Lessons from v1/v2:
- 1H trend entries have ~36% win rate → negative EV
- MR is the only profitable mode but marginal
- Avg Win = Avg Loss with trailing stops → need higher win rate

This version:
- Uses 4H for ALL signals (not just regime)
- Two modes:
  1. Supertrend trend: trade on 4H Supertrend direction flips
  2. BB MR: fade 4H BB extremes in low-ADX conditions
- 1H only used for precise entry timing within the 4H signal window
- Wider stops (3x ATR on 4H) for bigger moves
- Larger position sizes (higher conviction)
"""

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.indicator_engine import IndicatorEngine
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel
from src.risk.volatility_model import VolatilityModel
from src.execution.fee_calculator import FeeCalculator

DATA_DIR = PROJECT_ROOT / "user_data" / "data"
PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]
INITIAL_BALANCE = 68.33
HARD_FLOOR = 30.0


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def run_backtest():
    ie = IndicatorEngine()
    fee_calc = FeeCalculator()
    vol_model = VolatilityModel(forecast_horizon=1)

    data_1h = {}
    data_4h = {}
    for pair in PAIRS:
        data_1h[pair] = load_data(pair, "1h")
        data_4h[pair] = load_data(pair, "4h")

    balance = INITIAL_BALANCE
    peak_balance = balance
    trades = []
    equity_curve = []
    open_positions = []
    daily_pnl = {}

    # Pre-compute 4H indicators for efficiency
    data_4h_ind = {}
    for pair in PAIRS:
        data_4h_ind[pair] = ie.calculate_all(data_4h[pair].copy())

    min_1h_len = min(len(data_1h[p]) for p in PAIRS)
    start_idx = 200

    # Track 4H Supertrend direction changes
    prev_st_dir = {pair: None for pair in PAIRS}
    # Track last 4H candle index we processed
    last_4h_idx = {pair: -1 for pair in PAIRS}

    print(f"Backtesting v3 (4H Supertrend + MR) from {start_idx} to {min_1h_len}")
    print(f"Initial balance: ${balance:.2f}")
    print(f"{'='*60}")

    for i in range(start_idx, min_1h_len):
        if balance < HARD_FLOOR:
            print(f"\nHARD FLOOR HIT at ${balance:.2f}")
            break

        ts = data_1h[PAIRS[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance, "trades": 0}

        # --- Manage open positions ---
        closed_idx = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            hi = data_1h[pair]["high"].iloc[i]
            lo = data_1h[pair]["low"].iloc[i]
            cl = data_1h[pair]["close"].iloc[i]
            atr_val = pos["atr"]

            # Check Supertrend reversal exit (for trend trades)
            # Even though these are individual losers (-$33 in v3.0), they
            # free capital for the new direction trade → net system improvement
            st_exit = False
            if pos["strategy"] == "SupertrendTrend":
                current_ts = data_1h[pair]["timestamp"].iloc[i]
                df_4h = data_4h_ind[pair]
                df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
                if len(df_4h_valid) > 0:
                    current_st_dir = df_4h_valid["supertrend_direction"].iloc[-1]
                    if not np.isnan(current_st_dir):
                        if pos["direction"] == "long" and current_st_dir < 0:
                            st_exit = True
                        elif pos["direction"] == "short" and current_st_dir > 0:
                            st_exit = True

            # Trailing stop: activate after 2.0 ATR favorable, trail at 2.5 ATR
            if pos["direction"] == "long":
                if hi > pos.get("best_price", pos["entry"]):
                    pos["best_price"] = hi
                fav = pos["best_price"] - pos["entry"]
                if fav > 2.0 * atr_val:
                    new_sl = pos["best_price"] - 2.5 * atr_val
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl
            else:
                if lo < pos.get("best_price", pos["entry"]):
                    pos["best_price"] = lo
                fav = pos["entry"] - pos["best_price"]
                if fav > 2.0 * atr_val:
                    new_sl = pos["best_price"] + 2.5 * atr_val
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

            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and not hit_st and bars_held >= 120:  # 5 days
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
                    "timestamp": str(ts),
                    "bars_held": bars_held,
                })
                daily_pnl[day]["trades"] += 1
                closed_idx.append(pidx)

        for pidx in sorted(closed_idx, reverse=True):
            open_positions.pop(pidx)

        # --- CB check ---
        cb_level = CircuitBreaker.check_level(Decimal(str(balance)))
        if cb_level == CircuitBreakerLevel.DEAD:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        constraints = CircuitBreaker.get_constraints(Decimal(str(balance)))
        if len(open_positions) >= constraints.max_positions:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        open_pairs = {p["pair"] for p in open_positions}

        for pair in PAIRS:
            if pair in open_pairs:
                continue
            if len(open_positions) >= constraints.max_positions:
                break

            current_ts = data_1h[pair]["timestamp"].iloc[i]
            df_4h = data_4h_ind[pair]
            df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
            if len(df_4h_valid) < 100:
                continue

            # Current 4H indicator values
            st_dir = df_4h_valid["supertrend_direction"].iloc[-1]
            adx_4h = df_4h_valid["adx"].iloc[-1]
            atr_4h = df_4h_valid["atr"].iloc[-1]
            ema9_4h = df_4h_valid["ema_9"].iloc[-1]
            ema21_4h = df_4h_valid["ema_21"].iloc[-1]
            close_4h = df_4h_valid["close"].iloc[-1]
            rsi_4h = df_4h_valid["rsi"].iloc[-1]
            zscore_4h = df_4h_valid["zscore"].iloc[-1]
            bb_upper_4h = df_4h_valid["bb_upper"].iloc[-1]
            bb_lower_4h = df_4h_valid["bb_lower"].iloc[-1]
            bb_mid_4h = df_4h_valid["bb_middle"].iloc[-1]

            for v in [st_dir, adx_4h, atr_4h, ema9_4h, ema21_4h, close_4h]:
                if np.isnan(v):
                    continue

            # Get 1H data for entry timing
            df_1h = data_1h[pair].iloc[:i + 1]
            close_1h = df_1h["close"].iloc[-1]

            # Current 4H candle index
            cur_4h_idx = len(df_4h_valid) - 1

            signal = None

            # ===========================================================
            # SIGNAL 1: Supertrend Direction Flip (4H)
            # Buy when Supertrend flips bullish, sell when bearish
            # Only enter on the flip candle (not sustained direction)
            # ===========================================================
            if cur_4h_idx != last_4h_idx[pair]:
                last_4h_idx[pair] = cur_4h_idx
                prev_dir = prev_st_dir[pair]
                prev_st_dir[pair] = st_dir

                if prev_dir is not None and not np.isnan(prev_dir):
                    # Supertrend flipped bullish
                    if prev_dir < 0 and st_dir > 0 and adx_4h >= 18:
                        sl = close_1h - 3.0 * atr_4h
                        tp = close_1h + 6.0 * atr_4h  # Far TP, rely on trailing + ST reversal
                        confidence = 50.0
                        if adx_4h > 25:
                            confidence += 10.0
                        if ema9_4h > ema21_4h:
                            confidence += 10.0
                        if not np.isnan(rsi_4h) and 30 < rsi_4h < 65:
                            confidence += 5.0

                        signal = {
                            "direction": "long",
                            "entry": close_1h,
                            "sl": sl,
                            "tp": tp,
                            "confidence": confidence,
                            "strategy": "SupertrendTrend",
                            "atr": atr_4h,
                        }

                    # Supertrend flipped bearish
                    elif prev_dir > 0 and st_dir < 0 and adx_4h >= 18:
                        sl = close_1h + 3.0 * atr_4h
                        tp = close_1h - 6.0 * atr_4h
                        confidence = 50.0
                        if adx_4h > 25:
                            confidence += 10.0
                        if ema9_4h < ema21_4h:
                            confidence += 10.0
                        if not np.isnan(rsi_4h) and 35 < rsi_4h < 70:
                            confidence += 5.0

                        signal = {
                            "direction": "short",
                            "entry": close_1h,
                            "sl": sl,
                            "tp": tp,
                            "confidence": confidence,
                            "strategy": "SupertrendTrend",
                            "atr": atr_4h,
                        }

            # ===========================================================
            # SIGNAL 2: 4H BB Mean Reversion (ranging market)
            # Fade BB extremes when ADX < 22 on 4H
            # ===========================================================
            if signal is None and not np.isnan(adx_4h) and adx_4h < 22:
                if (not np.isnan(zscore_4h) and not np.isnan(rsi_4h) and
                        not np.isnan(bb_lower_4h) and not np.isnan(bb_mid_4h)):

                    # Long: 4H price at lower BB + zscore oversold
                    if zscore_4h <= -1.5 and rsi_4h < 35:
                        sl = close_1h - 2.0 * atr_4h
                        tp = bb_mid_4h  # Target: 4H middle BB
                        if tp <= close_1h:
                            tp = close_1h + 2.0 * atr_4h

                        rr = abs(tp - close_1h) / abs(close_1h - sl) if abs(close_1h - sl) > 0 else 0
                        if rr >= 1.5:
                            confidence = 40.0
                            if abs(zscore_4h) > 2.0:
                                confidence += 10.0
                            if rsi_4h < 25:
                                confidence += 10.0

                            signal = {
                                "direction": "long",
                                "entry": close_1h,
                                "sl": sl,
                                "tp": tp,
                                "confidence": confidence,
                                "strategy": "MeanReversion4H",
                                "atr": atr_4h,
                            }

                    # Short: 4H price at upper BB + zscore overbought
                    elif zscore_4h >= 1.5 and rsi_4h > 65:
                        sl = close_1h + 2.0 * atr_4h
                        tp = bb_mid_4h
                        if tp >= close_1h:
                            tp = close_1h - 2.0 * atr_4h

                        rr = abs(close_1h - tp) / abs(sl - close_1h) if abs(sl - close_1h) > 0 else 0
                        if rr >= 1.5:
                            confidence = 40.0
                            if zscore_4h > 2.0:
                                confidence += 10.0
                            if rsi_4h > 75:
                                confidence += 10.0

                            signal = {
                                "direction": "short",
                                "entry": close_1h,
                                "sl": sl,
                                "tp": tp,
                                "confidence": confidence,
                                "strategy": "MeanReversion4H",
                                "atr": atr_4h,
                            }

            if signal is None:
                continue

            # Validate SL/TP
            if signal["direction"] == "long":
                if signal["sl"] >= signal["entry"] or signal["tp"] <= signal["entry"]:
                    continue
            else:
                if signal["sl"] <= signal["entry"] or signal["tp"] >= signal["entry"]:
                    continue
            if signal["sl"] <= 0 or signal["tp"] <= 0:
                continue

            # Position sizing
            confidence = signal["confidence"]
            if confidence >= 60:
                base_leverage = 5
            elif confidence >= 45:
                base_leverage = 3
            else:
                base_leverage = 2

            # GARCH adjustment
            try:
                df_garch = data_1h[pair].iloc[:i + 1].tail(500).copy()
                vol_state = vol_model.forecast_simple(df_garch)
                leverage = VolatilityModel.adjust_leverage(
                    requested_leverage=base_leverage,
                    vol_state=vol_state,
                    max_leverage=int(constraints.max_leverage),
                )
            except Exception:
                leverage = min(base_leverage, int(constraints.max_leverage))

            if leverage == 0:
                leverage = 1

            cb_caps = {
                CircuitBreakerLevel.GREEN: 10,
                CircuitBreakerLevel.YELLOW: 5,
                CircuitBreakerLevel.RED: 3,
            }
            leverage = min(leverage, cb_caps.get(cb_level, 3))

            # Position sizing: higher conviction = bigger size
            # Use 15% for high confidence, 10% for medium, 7% for low
            if confidence >= 60:
                position_pct = 0.15
            elif confidence >= 45:
                position_pct = 0.10
            else:
                position_pct = 0.07
            position_pct *= float(constraints.size_multiplier)

            margin = balance * position_pct
            notional = margin * leverage

            min_notional = 20.0 if "ETH" in pair else 5.0
            if notional < min_notional:
                continue

            size = notional / signal["entry"]

            open_positions.append({
                "pair": pair,
                "direction": signal["direction"],
                "entry": signal["entry"],
                "sl": signal["sl"],
                "tp": signal["tp"],
                "size": size,
                "leverage": leverage,
                "margin": margin,
                "strategy": signal["strategy"],
                "entry_idx": i,
                "best_price": signal["entry"],
                "atr": signal["atr"],
            })

        equity_curve.append({"timestamp": str(ts), "balance": balance})
        daily_pnl[day]["end"] = balance

    # ─── Results ───
    print(f"\n{'='*60}")
    print("BACKTEST V3 RESULTS")
    print(f"{'='*60}")

    total_trades = len(trades)
    if total_trades == 0:
        print("No trades executed!")
        return

    wins = sum(1 for t in trades if t["is_win"])
    losses = total_trades - wins
    win_rate = wins / total_trades * 100
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_fees = sum(t["fees"] for t in trades)
    avg_win = np.mean([t["net_pnl"] for t in trades if t["is_win"]]) if wins > 0 else 0
    avg_loss = np.mean([abs(t["net_pnl"]) for t in trades if not t["is_win"]]) if losses > 0 else 0

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

    final_balance = balance
    total_return = (final_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    days = len(daily_pnl)

    print(f"Period: {days} days")
    print(f"Initial Balance: ${INITIAL_BALANCE:.2f}")
    print(f"Final Balance:   ${final_balance:.2f}")
    print(f"Total Return:    {total_return:+.2f}%")
    print(f"Total P&L:       ${total_pnl:+.2f}")
    print(f"Total Fees:      ${total_fees:.2f}")
    print()
    print(f"Total Trades:    {total_trades}")
    print(f"Wins:            {wins} ({win_rate:.1f}%)")
    print(f"Losses:          {losses} ({100 - win_rate:.1f}%)")
    print(f"Avg Win:         ${avg_win:+.2f}")
    print(f"Avg Loss:        ${avg_loss:.2f}")
    pf = (avg_win * wins) / (avg_loss * losses) if losses > 0 and avg_loss > 0 else 0
    print(f"Profit Factor:   {pf:.2f}")
    print()
    print(f"Max Drawdown:    {max_dd:.1f}%")
    print(f"Sharpe Ratio:    {sharpe:.2f}")
    print(f"Avg Daily Return:{avg_daily:+.3f}%")
    print()

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t["exit_reason"]
        if r not in exit_reasons:
            exit_reasons[r] = {"count": 0, "wins": 0, "pnl": 0.0}
        exit_reasons[r]["count"] += 1
        exit_reasons[r]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            exit_reasons[r]["wins"] += 1

    print("Exit Reason Breakdown:")
    for r, stats in sorted(exit_reasons.items()):
        wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
        print(f"  {r:8s}: {stats['count']:3d} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")

    # Strategy breakdown
    print("\nStrategy Breakdown:")
    strat_stats = {}
    for t in trades:
        s = t["strategy"]
        if s not in strat_stats:
            strat_stats[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
        strat_stats[s]["trades"] += 1
        strat_stats[s]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            strat_stats[s]["wins"] += 1

    for s, stats in sorted(strat_stats.items()):
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
        print(f"  {s:20s}: {stats['trades']:3d} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")

    # Pair breakdown
    print("\nPair Breakdown:")
    pair_stats = {}
    for t in trades:
        p = t["pair"]
        if p not in pair_stats:
            pair_stats[p] = {"trades": 0, "wins": 0, "pnl": 0.0}
        pair_stats[p]["trades"] += 1
        pair_stats[p]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            pair_stats[p]["wins"] += 1

    for p, stats in sorted(pair_stats.items()):
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
        print(f"  {p:20s}: {stats['trades']:3d} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")

    # Avg bars held
    avg_bars = np.mean([t["bars_held"] for t in trades])
    print(f"\nAvg Bars Held: {avg_bars:.0f} hours")

    # Save results
    results_dir = PROJECT_ROOT / "user_data" / "backtest_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / f"backtest_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results = {
        "version": "v3",
        "period_days": days,
        "initial_balance": INITIAL_BALANCE,
        "final_balance": round(final_balance, 2),
        "total_return_pct": round(total_return, 2),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe_ratio": round(sharpe, 2),
        "trades": trades,
    }
    results_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to: {results_file}")

    print(f"\n{'='*60}")
    print("PASS/FAIL CRITERIA:")
    checks = [
        ("Sharpe > 0.5", sharpe > 0.5),
        ("Max DD < 25%", max_dd < 25),
        ("Win Rate > 45%", win_rate > 45),
        ("Positive Total P&L", total_pnl > 0),
        ("Avg Daily > 0%", avg_daily > 0),
        ("Profit Factor > 1.0", pf > 1.0),
    ]
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")


if __name__ == "__main__":
    run_backtest()
