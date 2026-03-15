"""
Backtest v2 — Multi-timeframe pullback-in-trend strategy.

Key insight from v1 iterations: pure 1H trend following on crypto has
negative EV because EMA crossovers generate too many false signals.

This version uses:
- 4H for trend direction (EMA crossover + ADX)
- 1H for entry timing (pullback to support in trend direction)
- ATR-based position management

Three entry modes:
1. Trend pullback: 4H trending + 1H RSI pullback + price near EMA21
2. BB reversion: 4H ranging + 1H at Bollinger Band extreme
3. Momentum breakout: 4H trending + 1H volume surge above resistance

All modes use trailing stops and time-based exits.
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


def detect_4h_trend(df_4h: pd.DataFrame) -> tuple[str, float, float]:
    """Detect 4H trend direction and strength.

    Returns: (direction, adx, trend_strength)
    direction: 'long', 'short', or 'none'
    """
    ema9 = df_4h["ema_9"].iloc[-1]
    ema21 = df_4h["ema_21"].iloc[-1]
    ema50 = df_4h["ema_50"].iloc[-1]
    adx = df_4h["adx"].iloc[-1]

    if any(np.isnan(v) for v in [ema9, ema21, adx]):
        return "none", 0, 0

    # Trend strength: how aligned are the EMAs
    if ema9 > ema21:
        direction = "long"
        strength = 1.0
        if not np.isnan(ema50) and ema21 > ema50:
            strength = 2.0  # Strong bullish alignment
    elif ema9 < ema21:
        direction = "short"
        strength = 1.0
        if not np.isnan(ema50) and ema21 < ema50:
            strength = 2.0  # Strong bearish alignment
    else:
        direction = "none"
        strength = 0

    return direction, adx, strength


def check_1h_pullback_entry(df_1h: pd.DataFrame, trend_dir: str, adx_4h: float, trend_strength: float = 0.0) -> dict | None:
    """Check if 1H presents a pullback entry opportunity.

    Mode 1: Trend pullback — enter when 1H pulls back to EMA21 in trend direction
    Mode 2: BB reversion — enter at BB extreme when 4H is ranging
    Mode 3: Momentum continuation — MACD confirms trend after pullback
    """
    close = df_1h["close"].iloc[-1]
    rsi = df_1h["rsi"].iloc[-1]
    atr = df_1h["atr"].iloc[-1]
    ema9 = df_1h["ema_9"].iloc[-1]
    ema21 = df_1h["ema_21"].iloc[-1]
    bb_upper = df_1h["bb_upper"].iloc[-1]
    bb_lower = df_1h["bb_lower"].iloc[-1]
    bb_mid = df_1h["bb_middle"].iloc[-1]
    adx_1h = df_1h["adx"].iloc[-1]
    macd = df_1h["macd"].iloc[-1]
    macd_signal = df_1h["macd_signal"].iloc[-1]
    macd_hist = df_1h["macd_hist"].iloc[-1]
    volume = df_1h["volume"].iloc[-1]
    zscore = df_1h["zscore"].iloc[-1]
    supertrend_dir = df_1h["supertrend_direction"].iloc[-1]

    for v in [close, rsi, atr, ema21, bb_upper, bb_lower, adx_1h]:
        if np.isnan(v):
            return None

    if atr <= 0:
        return None

    vol_avg = df_1h["volume"].iloc[-20:].mean()
    vol_ratio = volume / vol_avg if vol_avg > 0 else 1.0

    # Previous bar values for momentum detection
    prev_macd_hist = df_1h["macd_hist"].iloc[-2] if len(df_1h) >= 2 else np.nan
    prev_rsi = df_1h["rsi"].iloc[-2] if len(df_1h) >= 2 else np.nan

    # ===================================================================
    # MODE 1: TREND PULLBACK (trailing stop only, no fixed TP)
    # 4H trending + 1H bounce off EMA21 + Supertrend aligned
    # ===================================================================
    if adx_4h >= 25 and trend_strength >= 2.0:  # Only strong trends
        if trend_dir == "long":
            prev_close = df_1h["close"].iloc[-2] if len(df_1h) >= 2 else np.nan
            # Confirmed bounce: was at/below EMA21, now above
            bounced = (close > ema21 and supertrend_dir > 0 and
                       not np.isnan(prev_close) and prev_close <= ema21 + 0.2 * atr)
            rsi_ok = 35 < rsi < 55  # Not overbought, recovered from pullback

            if bounced and rsi_ok:
                sl = close - 2.0 * atr
                tp = close + 5.0 * atr  # Far TP, rely on trailing stop mostly
                confidence = 50.0
                if vol_ratio < 0.7:
                    confidence += 5.0
                if adx_4h > 30:
                    confidence += 10.0

                return {
                    "direction": "long",
                    "entry": close,
                    "sl": sl,
                    "tp": tp,
                    "confidence": confidence,
                    "strategy": "TrendPullback",
                    "atr": atr,
                }

        elif trend_dir == "short":
            prev_close = df_1h["close"].iloc[-2] if len(df_1h) >= 2 else np.nan
            bounced = (close < ema21 and supertrend_dir < 0 and
                       not np.isnan(prev_close) and prev_close >= ema21 - 0.2 * atr)
            rsi_ok = 45 < rsi < 65

            if bounced and rsi_ok:
                sl = close + 2.0 * atr
                tp = close - 5.0 * atr
                confidence = 50.0
                if vol_ratio < 0.7:
                    confidence += 5.0
                if adx_4h > 30:
                    confidence += 10.0

                return {
                    "direction": "short",
                    "entry": close,
                    "sl": sl,
                    "tp": tp,
                    "confidence": confidence,
                    "strategy": "TrendPullback",
                    "atr": atr,
                }

    # ===================================================================
    # MODE 2: MEAN REVERSION (ranging market)
    # 4H ADX < 22 + 1H at BB extreme
    # ===================================================================
    if adx_4h < 25:  # Wider threshold — MR is the most profitable mode
        if zscore <= -1.5 and rsi < 35 and close <= bb_lower + 0.3 * atr:
            sl = close - 1.5 * atr
            tp = bb_mid  # Target: middle BB
            if tp <= close:
                tp = close + 2.0 * atr

            rr = abs(tp - close) / abs(close - sl) if abs(close - sl) > 0 else 0
            if rr >= 1.5:
                confidence = 35.0
                if abs(zscore) > 2.0:
                    confidence += 10.0
                if rsi < 25:
                    confidence += 10.0
                if vol_ratio < 0.7:
                    confidence += 5.0

                return {
                    "direction": "long",
                    "entry": close,
                    "sl": sl,
                    "tp": tp,
                    "confidence": confidence,
                    "strategy": "MeanReversion",
                    "atr": atr,
                }

        elif zscore >= 1.5 and rsi > 65 and close >= bb_upper - 0.3 * atr:
            sl = close + 1.5 * atr
            tp = bb_mid
            if tp >= close:
                tp = close - 2.0 * atr

            rr = abs(close - tp) / abs(sl - close) if abs(sl - close) > 0 else 0
            if rr >= 1.5:
                confidence = 35.0
                if zscore > 2.0:
                    confidence += 10.0
                if rsi > 75:
                    confidence += 10.0
                if vol_ratio < 0.7:
                    confidence += 5.0

                return {
                    "direction": "short",
                    "entry": close,
                    "sl": sl,
                    "tp": tp,
                    "confidence": confidence,
                    "strategy": "MeanReversion",
                    "atr": atr,
                }

    # ===================================================================
    # MODE 3: MOMENTUM BREAKOUT
    # Volume surge + MACD + Supertrend + Price closes above prev high
    # More selective: require close above previous bar's high/low
    # ===================================================================
    if vol_ratio >= 1.5 and not np.isnan(macd_hist) and not np.isnan(prev_macd_hist):
        prev_high = df_1h["high"].iloc[-2] if len(df_1h) >= 2 else np.nan
        prev_low = df_1h["low"].iloc[-2] if len(df_1h) >= 2 else np.nan

        if (macd_hist > 0 and macd_hist > prev_macd_hist and supertrend_dir > 0
                and close > ema21 and adx_1h > 22
                and not np.isnan(prev_high) and close > prev_high):  # Breakout bar
            sl = close - 1.5 * atr
            tp = close + 2.5 * atr  # Reduced from 4x — more achievable
            confidence = 45.0
            if vol_ratio > 2.0:
                confidence += 10.0
            if adx_1h > 30:
                confidence += 10.0

            return {
                "direction": "long",
                "entry": close,
                "sl": sl,
                "tp": tp,
                "confidence": confidence,
                "strategy": "Momentum",
                "atr": atr,
            }

        elif (macd_hist < 0 and macd_hist < prev_macd_hist and supertrend_dir < 0
                and close < ema21 and adx_1h > 22
                and not np.isnan(prev_low) and close < prev_low):
            sl = close + 1.5 * atr
            tp = close - 2.5 * atr
            confidence = 45.0
            if vol_ratio > 2.0:
                confidence += 10.0
            if adx_1h > 30:
                confidence += 10.0

            return {
                "direction": "short",
                "entry": close,
                "sl": sl,
                "tp": tp,
                "confidence": confidence,
                "strategy": "Momentum",
                "atr": atr,
            }

    return None


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

    min_1h_len = min(len(data_1h[p]) for p in PAIRS)
    start_idx = 200

    print(f"Backtesting v2 (pullback-in-trend) from {start_idx} to {min_1h_len}")
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

            # Trailing stop: activate after 1.5 ATR favorable, trail at 2.0 ATR
            if pos["direction"] == "long":
                if hi > pos.get("best_price", pos["entry"]):
                    pos["best_price"] = hi
                fav = pos["best_price"] - pos["entry"]
                if fav > 1.5 * atr_val:
                    new_sl = pos["best_price"] - 2.0 * atr_val
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl
            else:
                if lo < pos.get("best_price", pos["entry"]):
                    pos["best_price"] = lo
                fav = pos["entry"] - pos["best_price"]
                if fav > 1.5 * atr_val:
                    new_sl = pos["best_price"] + 2.0 * atr_val
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
            if not hit_sl and not hit_tp and bars_held >= 48:
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
                    "exit_reason": "TP" if hit_tp else ("TIME" if hit_time else "SL"),
                    "strategy": pos["strategy"],
                    "timestamp": str(ts),
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

            df_1h = data_1h[pair].iloc[:i + 1].copy()
            if len(df_1h) < 200:
                continue

            current_ts = df_1h["timestamp"].iloc[-1]
            df_4h = data_4h[pair][data_4h[pair]["timestamp"] <= current_ts].copy()
            if len(df_4h) < 100:
                continue

            try:
                df_4h_ind = ie.calculate_all(df_4h.tail(200).copy())
                df_1h_ind = ie.calculate_all(df_1h.tail(200).copy())
            except Exception:
                continue

            # 4H trend detection
            trend_dir, adx_4h, trend_strength = detect_4h_trend(df_4h_ind)

            # 1H entry check
            entry = check_1h_pullback_entry(df_1h_ind, trend_dir, adx_4h, trend_strength)
            if entry is None:
                continue

            # Validate SL/TP direction
            if entry["direction"] == "long":
                if entry["sl"] >= entry["entry"] or entry["tp"] <= entry["entry"]:
                    continue
            else:
                if entry["sl"] <= entry["entry"] or entry["tp"] >= entry["entry"]:
                    continue

            # Ensure positive prices
            if entry["sl"] <= 0 or entry["tp"] <= 0:
                continue

            # Position sizing
            confidence = entry["confidence"]

            # Leverage based on confidence and regime
            if confidence >= 50:
                base_leverage = 5
            elif confidence >= 40:
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

            # CB cap
            cb_caps = {
                CircuitBreakerLevel.GREEN: 10,
                CircuitBreakerLevel.YELLOW: 5,
                CircuitBreakerLevel.RED: 3,
            }
            leverage = min(leverage, cb_caps.get(cb_level, 3))

            # Half-Kelly sizing
            win_rate = 0.5
            if len(trades) >= 10:
                recent = trades[-30:]
                win_rate = sum(1 for t in recent if t["is_win"]) / len(recent)
                win_rate = max(0.3, min(0.7, win_rate))  # Clip to reasonable range

            rr_ratio = abs(entry["tp"] - entry["entry"]) / abs(entry["entry"] - entry["sl"])
            kelly = (win_rate * rr_ratio - (1 - win_rate)) / rr_ratio
            half_kelly = max(0.02, kelly * 0.5)  # Minimum 2% position
            position_pct = min(half_kelly, 0.15)
            position_pct *= float(constraints.size_multiplier)

            margin = balance * position_pct
            notional = margin * leverage

            # Minimum notional check
            min_notional = 20.0 if "ETH" in pair else 5.0
            if notional < min_notional:
                continue

            size = notional / entry["entry"]

            open_positions.append({
                "pair": pair,
                "direction": entry["direction"],
                "entry": entry["entry"],
                "sl": entry["sl"],
                "tp": entry["tp"],
                "size": size,
                "leverage": leverage,
                "margin": margin,
                "strategy": entry["strategy"],
                "entry_idx": i,
                "best_price": entry["entry"],
                "atr": entry["atr"],
            })

        equity_curve.append({"timestamp": str(ts), "balance": balance})
        daily_pnl[day]["end"] = balance

    # ─── Results ───
    print(f"\n{'='*60}")
    print("BACKTEST V2 RESULTS")
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
    for day, dpnl in sorted(daily_pnl.items()):
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
        print(f"  {r:6s}: {stats['count']:3d} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")

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

    # Save results
    results = {
        "version": "v2",
        "period_days": days,
        "initial_balance": INITIAL_BALANCE,
        "final_balance": round(final_balance, 2),
        "total_return_pct": round(total_return, 2),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe_ratio": round(sharpe, 2),
        "avg_daily_return_pct": round(avg_daily, 3),
        "strategy_breakdown": {k: v for k, v in strat_stats.items()},
        "exit_reasons": {k: v for k, v in exit_reasons.items()},
        "trades": trades,
    }

    results_dir = PROJECT_ROOT / "user_data" / "backtest_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / f"backtest_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to: {results_file}")

    print(f"\n{'='*60}")
    print("PASS/FAIL CRITERIA:")
    checks = [
        ("Sharpe > 0.5", sharpe > 0.5),
        ("Max DD < 25%", max_dd < 25),
        ("Win Rate > 45%", win_rate > 45),
        ("Trades > 50", total_trades > 50),
        ("Positive Total P&L", total_pnl > 0),
        ("Avg Daily > 0%", avg_daily > 0),
        ("Profit Factor > 1.0", pf > 1.0),
    ]
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")


if __name__ == "__main__":
    run_backtest()
