"""
Focused analysis: What does the FIRST 21 DAYS look like trade-by-trade?
Run the best aggressive config and print every single trade in detail.
Also: what daily% would be needed at different starting capitals.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.indicator_engine import IndicatorEngine
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel, TradeResult
from src.risk.leverage_manager import LeverageManager
from src.risk.volatility_model import VolatilityModel
from src.execution.fee_calculator import FeeCalculator
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.cross_asset_consensus import CrossAssetConsensus

DATA_DIR = PROJECT_ROOT / "user_data" / "data"
ALL_PAIRS = [
    "BTC_USDT_USDT", "ETH_USDT_USDT", "SOL_USDT_USDT",
    "DOGE_USDT_USDT", "XRP_USDT_USDT", "LINK_USDT_USDT",
    "AVAX_USDT_USDT", "SUI_USDT_USDT", "ADA_USDT_USDT",
]
INITIAL_BALANCE = 68.33
HARD_FLOOR = 30.0
MAX_HOLD_BARS = 100
MAX_POS_PCT = 0.25
TRAIL_ACTIVATE_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.5

def load_data(pair, tf):
    path = DATA_DIR / f"{pair}_{tf}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

def get_min_notional(pair):
    if "BTC" in pair: return 100.0
    if "ETH" in pair: return 20.0
    return 5.0

def main():
    ie = IndicatorEngine()
    fee_calc = FeeCalculator()
    vol_model = VolatilityModel(forecast_horizon=1)
    adaptive = AdaptiveStrategy()
    consensus = CrossAssetConsensus()

    data_1h, data_4h, data_4h_ind = {}, {}, {}
    for pair in ALL_PAIRS:
        data_1h[pair] = load_data(pair, "1h")
        data_4h[pair] = load_data(pair, "4h")
        data_4h_ind[pair] = ie.calculate_all(data_4h[pair].copy())

    balance = INITIAL_BALANCE
    trades = []
    open_positions = []
    recent_trade_results = []
    daily_pnl = {}

    min_1h_len = min(len(data_1h[p]) for p in ALL_PAIRS)
    start_idx = 200
    day_count = 0

    print("=" * 90)
    print("FIRST 30 DAYS — TRADE-BY-TRADE DETAIL (AGG4: 25% sizing, 100-bar hold, 9 pairs)")
    print(f"Starting balance: ${INITIAL_BALANCE}")
    print("=" * 90)

    for i in range(start_idx, min_1h_len):
        if balance < HARD_FLOOR:
            break

        ts = data_1h[ALL_PAIRS[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance}
            day_count = len(daily_pnl)
            if day_count > 30:
                break

        # Manage positions
        closed_idx = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            hi = data_1h[pair]["high"].iloc[i]
            lo = data_1h[pair]["low"].iloc[i]
            cl = data_1h[pair]["close"].iloc[i]
            atr_val = pos["atr"]

            if pos["strategy"] == "SupertrendTrend":
                current_ts = data_1h[pair]["timestamp"].iloc[i]
                df_4h = data_4h_ind[pair]
                df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
                if len(df_4h_valid) > 0:
                    should_exit = adaptive.check_supertrend_reversal(df_4h_valid, pos["direction"])
                    if should_exit:
                        if pos["direction"] == "long":
                            pos["sl"] = max(pos["sl"], pos["entry"])
                        else:
                            pos["sl"] = min(pos["sl"], pos["entry"])

            if pos["direction"] == "long":
                if hi > pos.get("best_price", pos["entry"]):
                    pos["best_price"] = hi
                fav = pos["best_price"] - pos["entry"]
                if fav > TRAIL_ACTIVATE_ATR_MULT * atr_val:
                    new_sl = pos["best_price"] - TRAIL_ATR_MULT * atr_val
                    if new_sl > pos["sl"]: pos["sl"] = new_sl
            else:
                if lo < pos.get("best_price", pos["entry"]):
                    pos["best_price"] = lo
                fav = pos["entry"] - pos["best_price"]
                if fav > TRAIL_ACTIVATE_ATR_MULT * atr_val:
                    new_sl = pos["best_price"] + TRAIL_ATR_MULT * atr_val
                    if new_sl < pos["sl"]: pos["sl"] = new_sl

            hit_sl = hit_tp = hit_time = False
            exit_price = None
            if pos["direction"] == "long":
                if lo <= pos["sl"]: hit_sl, exit_price = True, pos["sl"]
                elif hi >= pos["tp"]: hit_tp, exit_price = True, pos["tp"]
            else:
                if hi >= pos["sl"]: hit_sl, exit_price = True, pos["sl"]
                elif lo <= pos["tp"]: hit_tp, exit_price = True, pos["tp"]

            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and bars_held >= MAX_HOLD_BARS:
                hit_time, exit_price = True, cl

            if hit_sl or hit_tp or hit_time:
                if pos["direction"] == "long":
                    raw_pnl = (exit_price - pos["entry"]) * pos["size"]
                else:
                    raw_pnl = (pos["entry"] - exit_price) * pos["size"]
                entry_fees = float(fee_calc.calculate_fees(Decimal(str(pos["entry"] * pos["size"])), is_maker=False))
                exit_fees = float(fee_calc.calculate_fees(Decimal(str(exit_price * pos["size"])), is_maker=False))
                net_pnl = raw_pnl - entry_fees - exit_fees
                balance += net_pnl

                exit_reason = "TP" if hit_tp else ("TIME" if hit_time else "SL")
                roi = (net_pnl / pos["margin"]) * 100 if pos["margin"] > 0 else 0
                trades.append({"ts": str(ts), "pair": pair, "dir": pos["direction"],
                               "entry": pos["entry"], "exit": exit_price, "pnl": net_pnl,
                               "margin": pos["margin"], "lev": pos["leverage"],
                               "roi": roi, "reason": exit_reason, "strat": pos["strategy"],
                               "bars": bars_held})
                closed_idx.append(pidx)
                recent_trade_results.append(TradeResult(is_win=net_pnl > 0,
                    closed_at=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts))
                recent_trade_results = recent_trade_results[-10:]

                win_str = "WIN " if net_pnl > 0 else "LOSS"
                print(f"  [{ts.strftime('%Y-%m-%d %H:%M')}] EXIT {pair:15s} {pos['direction']:5s} "
                      f"@ {exit_price:.4f} ({exit_reason:4s}) | {win_str} ${net_pnl:+.2f} "
                      f"(ROI {roi:+.1f}%) margin=${pos['margin']:.2f} x{pos['leverage']} | "
                      f"Bal=${balance:.2f} | held {bars_held}bars")

        for pidx in sorted(closed_idx, reverse=True):
            open_positions.pop(pidx)

        # CB
        start_of_day_balance = Decimal(str(daily_pnl[day]["start"]))
        cb_state = CircuitBreaker.is_trading_allowed(
            balance=Decimal(str(balance)), recent_trades=recent_trade_results,
            start_of_day_balance=start_of_day_balance)

        if cb_state.level == CircuitBreakerLevel.DEAD:
            daily_pnl[day]["end"] = balance; continue
        if not cb_state.constraints.trading_allowed:
            daily_pnl[day]["end"] = balance; continue
        constraints = cb_state.constraints
        if len(open_positions) >= constraints.max_positions:
            daily_pnl[day]["end"] = balance; continue

        open_pairs = {p["pair"] for p in open_positions}

        current_ts = data_1h[ALL_PAIRS[0]]["timestamp"].iloc[i]
        pair_4h_slices = {}
        for p in ALL_PAIRS:
            _df = data_4h_ind[p]
            _valid = _df[_df["timestamp"] <= current_ts]
            if len(_valid) >= 100: pair_4h_slices[p] = _valid
        consensus_adj = consensus.compute(pair_4h_slices)

        for pair in ALL_PAIRS:
            if pair in open_pairs: continue
            if len(open_positions) >= constraints.max_positions: break

            current_ts = data_1h[pair]["timestamp"].iloc[i]
            df_4h = data_4h_ind[pair]
            df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
            if len(df_4h_valid) < 100: continue

            df_1h_slice = data_1h[pair].iloc[:i + 1].copy()
            df_1h_ind = ie.calculate_all(df_1h_slice.tail(200).copy())

            signal = adaptive.get_signal_multi_tf(df_4h_valid, df_1h_ind)
            if signal is None: continue

            adj = consensus_adj.get(pair, 0.0)
            if adj != 0.0:
                adjusted_conf = max(0.0, min(100.0, signal.confidence + adj))
                signal = signal.model_copy(update={"confidence": adjusted_conf})

            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence, regime=signal.regime,
                circuit_breaker_level=cb_state.level)
            leverage = leverage_result.leverage
            if leverage == 0: continue

            try:
                df_garch = data_1h[pair].iloc[:i + 1].tail(500).copy()
                vol_state = vol_model.forecast(df_garch)
                if vol_state is None: vol_state = vol_model.forecast_simple(df_garch)
                leverage = VolatilityModel.adjust_leverage(
                    requested_leverage=leverage, vol_state=vol_state,
                    max_leverage=constraints.max_leverage)
            except: leverage = min(leverage, constraints.max_leverage)
            if leverage == 0: leverage = 1

            confidence = signal.confidence
            if confidence >= 60: position_pct = MAX_POS_PCT
            elif confidence >= 45: position_pct = MAX_POS_PCT * 0.667
            else: position_pct = MAX_POS_PCT * 0.467
            position_pct *= float(constraints.size_multiplier)
            margin = balance * position_pct
            max_margin = balance * MAX_POS_PCT
            if margin > max_margin: margin = max_margin
            if margin < 5.0:
                if balance < 5.0: continue
                margin = 5.0
            notional = margin * leverage
            min_notional = get_min_notional(pair)
            if notional < min_notional: continue
            size = notional / signal.entry_price
            atr_4h = float(df_4h_valid["atr"].dropna().iloc[-1]) if "atr" in df_4h_valid.columns else 0.0

            open_positions.append({
                "pair": pair, "direction": signal.direction.value,
                "entry": signal.entry_price, "sl": signal.stop_loss,
                "tp": signal.take_profit, "size": size, "leverage": leverage,
                "margin": margin, "strategy": signal.strategy_name,
                "confidence": signal.confidence, "entry_idx": i,
                "best_price": signal.entry_price, "atr": atr_4h})

            print(f"  [{ts.strftime('%Y-%m-%d %H:%M')}] ENTER {pair:15s} {signal.direction.value:5s} "
                  f"@ {signal.entry_price:.4f} x{leverage} margin=${margin:.2f} "
                  f"conf={signal.confidence:.0f}% SL={signal.stop_loss:.4f} TP={signal.take_profit:.4f} "
                  f"strat={signal.strategy_name}")

        daily_pnl[day]["end"] = balance

    # Daily summary
    print(f"\n{'='*90}")
    print("DAILY P&L SUMMARY (First 30 days)")
    print(f"{'='*90}")
    cumulative = 0
    for idx, (day, dpnl) in enumerate(sorted(daily_pnl.items()), 1):
        day_ret = (dpnl["end"] - dpnl["start"]) / dpnl["start"] * 100 if dpnl["start"] > 0 else 0
        cumulative = (dpnl["end"] - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        bar = "█" * max(0, int(dpnl["end"] - INITIAL_BALANCE))
        print(f"  Day {idx:>2d} ({day}): ${dpnl['start']:>7.2f} -> ${dpnl['end']:>7.2f} "
              f"({day_ret:+5.2f}%) cumul={cumulative:+.1f}%")

    # FEASIBILITY MATH
    print(f"\n{'='*90}")
    print("MATHEMATICAL FEASIBILITY ANALYSIS")
    print(f"{'='*90}")

    targets = [
        (68.33, 1000, 21, "User target"),
        (68.33, 500, 21, "Half target"),
        (68.33, 200, 21, "Modest target"),
        (68.33, 1000, 60, "60-day target"),
        (68.33, 1000, 90, "90-day target"),
        (68.33, 1000, 120, "120-day target"),
        (200, 1000, 21, "With $200 start"),
        (500, 1000, 21, "With $500 start"),
    ]

    for start, target, days, label in targets:
        required_daily = ((target / start) ** (1/days) - 1) * 100
        multiple = target / start
        print(f"  {label:20s}: ${start:.0f} -> ${target:.0f} in {days}d = "
              f"{multiple:.1f}x = {required_daily:.2f}%/day compound needed")

    print(f"\n  ACHIEVABLE (from backtest):")
    print(f"  Best 21-day compound rate: 1.59%/day (AGG4)")
    print(f"  Best overall rate:         2.68%/day (AGG4)")
    print(f"")
    print(f"  At 1.59%/day for 21 days:  ${68.33 * (1.0159**21):.2f}")
    print(f"  At 2.68%/day for 21 days:  ${68.33 * (1.0268**21):.2f}")
    print(f"  At 2.68%/day for 60 days:  ${68.33 * (1.0268**60):.2f}")
    print(f"  At 2.68%/day for 90 days:  ${68.33 * (1.0268**90):.2f}")
    print(f"  At 2.68%/day for 119 days: ${68.33 * (1.0268**119):.2f}")

    print(f"\n{'='*90}")
    print("RISK OF RUIN ANALYSIS")
    print(f"{'='*90}")
    configs_risk = [
        ("15% sizing (current)", 0.15, 3.4),
        ("20% sizing", 0.20, 6.6),
        ("25% sizing", 0.25, 8.2),
        ("30% sizing", 0.30, 20.6),
    ]
    for label, pct, dd in configs_risk:
        worst_run = INITIAL_BALANCE * (1 - dd/100)
        margin_to_floor = ((worst_run - HARD_FLOOR) / worst_run) * 100
        print(f"  {label:25s}: MaxDD={dd:.1f}% -> worst balance=${worst_run:.2f} "
              f"(${worst_run-HARD_FLOOR:.2f} above $30 floor, {margin_to_floor:.1f}% margin)")


if __name__ == "__main__":
    main()
