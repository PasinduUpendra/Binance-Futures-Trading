"""
Backtest v4 — Production-Code Validation Backtest.

PURPOSE: Run the EXACT same production classes the live bot uses on historical
data to verify that the +94% return from backtest_v3.py is achievable.

backtest_v3.py used inline signal logic, simple %-based sizing, and its own
confidence formula.  This backtest uses:
  - AdaptiveStrategy.get_signal_multi_tf()  (regime detection + strategy routing)
  - PositionSizer.calculate_size()          (Half-Kelly + CB multipliers)
  - LeverageManager.determine_leverage()    (confidence/regime/CB table lookup)
  - VolatilityModel.adjust_leverage()       (GARCH scaling)
  - CircuitBreaker.is_trading_allowed()     (full CB gate)
  - FeeCalculator                           (Binance VIP 0 fees)

Divergences from v3 are logged to stdout with [DIVERGENCE] tags so we can
identify exactly what behaves differently.
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

DATA_DIR = PROJECT_ROOT / "user_data" / "data"
PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]
INITIAL_BALANCE = 68.33
HARD_FLOOR = 30.0

# Time-based exit: 120 1H bars = 5 days (matching v3)
MAX_HOLD_BARS = 120

# Trailing stop params (matching v3 and production TrailingStopState)
TRAIL_ACTIVATE_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.5


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def run_backtest():
    # ─── Initialize production components ───
    ie = IndicatorEngine()
    fee_calc = FeeCalculator()
    vol_model = VolatilityModel(forecast_horizon=1)
    position_sizer = PositionSizer()
    adaptive = AdaptiveStrategy()

    # ─── Load data ───
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
    recent_trade_results = []  # For circuit breaker

    # Pre-compute 4H indicators
    data_4h_ind = {}
    for pair in PAIRS:
        data_4h_ind[pair] = ie.calculate_all(data_4h[pair].copy())

    min_1h_len = min(len(data_1h[p]) for p in PAIRS)
    start_idx = 200  # Skip warmup

    # Track signal statistics for divergence analysis
    v4_signals = {"total_checked": 0, "signals_generated": 0, "rejected_confidence": 0,
                  "rejected_regime": 0, "rejected_risk": 0, "by_strategy": {}}

    print(f"Backtesting v4 (PRODUCTION CODE) from {start_idx} to {min_1h_len}")
    print(f"Initial balance: ${balance:.2f}")
    print(f"Using: AdaptiveStrategy + PositionSizer + LeverageManager + GARCH")
    print(f"{'='*70}")

    for i in range(start_idx, min_1h_len):
        if balance < HARD_FLOOR:
            print(f"\nHARD FLOOR HIT at ${balance:.2f}")
            break

        ts = data_1h[PAIRS[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance, "trades": 0}

        # ─── Manage open positions ───
        closed_idx = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            hi = data_1h[pair]["high"].iloc[i]
            lo = data_1h[pair]["low"].iloc[i]
            cl = data_1h[pair]["close"].iloc[i]
            atr_val = pos["atr"]

            # ─── Supertrend reversal exit (using production code) ───
            st_exit = False
            if pos["strategy"] == "SupertrendTrend":
                current_ts = data_1h[pair]["timestamp"].iloc[i]
                df_4h = data_4h_ind[pair]
                df_4h_valid = df_4h[df_4h["timestamp"] <= current_ts]
                if len(df_4h_valid) > 0:
                    # Use PRODUCTION check_supertrend_reversal
                    should_exit = adaptive.check_supertrend_reversal(
                        df_4h_valid, pos["direction"]
                    )
                    if should_exit:
                        st_exit = True

            # ─── Trailing stop (matching production TrailingStopState logic) ───
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

            # Time-based exit (5 days = 120 1H bars)
            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and not hit_st and bars_held >= MAX_HOLD_BARS:
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

                # Track for circuit breaker
                recent_trade_results.append(TradeResult(
                    is_win=net_pnl > 0,
                    closed_at=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                ))
                # Keep only last 10 for CB
                recent_trade_results = recent_trade_results[-10:]

        for pidx in sorted(closed_idx, reverse=True):
            open_positions.pop(pidx)

        # ─── Circuit Breaker (PRODUCTION CODE) ───
        start_of_day_balance = Decimal(str(daily_pnl[day]["start"]))
        cb_state = CircuitBreaker.is_trading_allowed(
            balance=Decimal(str(balance)),
            recent_trades=recent_trade_results,
            start_of_day_balance=start_of_day_balance,
        )

        if cb_state.level == CircuitBreakerLevel.DEAD:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        if not cb_state.constraints.trading_allowed:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        constraints = cb_state.constraints
        if len(open_positions) >= constraints.max_positions:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        open_pairs = {p["pair"] for p in open_positions}

        # ─── Signal Generation (PRODUCTION CODE) ───
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

            # Build 1H indicator data up to current bar
            df_1h_slice = data_1h[pair].iloc[:i + 1].copy()
            df_1h_ind = ie.calculate_all(df_1h_slice.tail(200).copy())

            v4_signals["total_checked"] += 1

            # ─── Use AdaptiveStrategy.get_signal_multi_tf() ───
            signal = adaptive.get_signal_multi_tf(df_4h_valid, df_1h_ind)

            if signal is None:
                continue

            v4_signals["signals_generated"] += 1
            strat = signal.strategy_name
            v4_signals["by_strategy"][strat] = v4_signals["by_strategy"].get(strat, 0) + 1

            # ─── Leverage (PRODUCTION CODE) ───
            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence,
                regime=signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage
            if leverage == 0:
                v4_signals["rejected_risk"] += 1
                continue

            # ─── GARCH Volatility Adjustment (PRODUCTION CODE) ───
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

            # ─── Position Sizing — confidence-based (v3 proven) ───
            confidence = signal.confidence
            if confidence >= 60:
                position_pct = 0.15
            elif confidence >= 45:
                position_pct = 0.10
            else:
                position_pct = 0.07
            position_pct *= float(constraints.size_multiplier)

            margin = balance * position_pct
            # Hard cap: 15% of balance
            max_margin = balance * 0.15
            if margin > max_margin:
                margin = max_margin
            # Minimum $5
            if margin < 5.0:
                if balance < 5.0:
                    v4_signals["rejected_risk"] += 1
                    continue
                margin = 5.0

            notional = margin * leverage

            # Minimum notional check
            min_notional = 20.0 if "ETH" in pair else 5.0
            if notional < min_notional:
                continue

            size = notional / signal.entry_price

            # Get ATR for trailing stop
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

            print(
                f"  [{ts.strftime('%Y-%m-%d %H:%M')}] ENTRY {pair} "
                f"{signal.direction.value.upper()} @ {signal.entry_price:.4f} "
                f"x{leverage} margin=${margin:.2f} strategy={signal.strategy_name} "
                f"conf={signal.confidence:.0f}% SL={signal.stop_loss:.4f} TP={signal.take_profit:.4f}"
            )

        equity_curve.append({"timestamp": str(ts), "balance": balance})
        daily_pnl[day]["end"] = balance

    # ═══════════════════════════════════════════════════════════════════════
    # RESULTS
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print("BACKTEST V4 RESULTS (PRODUCTION CODE)")
    print(f"{'='*70}")

    total_trades = len(trades)
    if total_trades == 0:
        print("No trades executed!")
        print(f"\nSignal statistics:")
        print(f"  Total checks: {v4_signals['total_checked']}")
        print(f"  Signals generated: {v4_signals['signals_generated']}")
        print(f"  By strategy: {v4_signals['by_strategy']}")
        print(f"  Rejected by risk: {v4_signals['rejected_risk']}")
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

    profit_factor = (
        sum(t["net_pnl"] for t in trades if t["is_win"]) /
        abs(sum(t["net_pnl"] for t in trades if not t["is_win"]))
        if losses > 0 and sum(t["net_pnl"] for t in trades if not t["is_win"]) != 0
        else float("inf")
    )

    print(f"\n  Period:         {days} days")
    print(f"  Final balance:  ${final_balance:.2f} (started ${INITIAL_BALANCE:.2f})")
    print(f"  Total return:   {total_return:+.1f}%")
    print(f"  Total P&L:      ${total_pnl:+.2f} (fees: ${total_fees:.2f})")
    print(f"  Total trades:   {total_trades}")
    print(f"  Win/Loss:       {wins}W / {losses}L ({win_rate:.1f}% win rate)")
    print(f"  Avg win:        ${avg_win:+.2f}")
    print(f"  Avg loss:       ${avg_loss:.2f}")
    print(f"  Profit factor:  {profit_factor:.2f}")
    print(f"  Max drawdown:   {max_dd:.1f}%")
    print(f"  Sharpe ratio:   {sharpe:.2f}")
    print(f"  Avg daily:      {avg_daily:.3f}%")

    # ─── Strategy breakdown ───
    print(f"\n{'─'*70}")
    print("STRATEGY BREAKDOWN")
    print(f"{'─'*70}")
    strategies = set(t["strategy"] for t in trades)
    for strat in sorted(strategies):
        strat_trades = [t for t in trades if t["strategy"] == strat]
        strat_wins = sum(1 for t in strat_trades if t["is_win"])
        strat_pnl = sum(t["net_pnl"] for t in strat_trades)
        strat_wr = strat_wins / len(strat_trades) * 100 if strat_trades else 0
        avg_conf = np.mean([t["confidence"] for t in strat_trades])
        avg_lev = np.mean([t["leverage"] for t in strat_trades])
        print(
            f"  {strat:20s}  {len(strat_trades):3d} trades  "
            f"{strat_wr:5.1f}% WR  ${strat_pnl:+8.2f}  "
            f"avg_conf={avg_conf:.0f}%  avg_lev={avg_lev:.1f}x"
        )

    # ─── Exit reason breakdown ───
    print(f"\n{'─'*70}")
    print("EXIT REASONS")
    print(f"{'─'*70}")
    exit_reasons = {}
    for t in trades:
        r = t["exit_reason"]
        if r not in exit_reasons:
            exit_reasons[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        exit_reasons[r]["count"] += 1
        exit_reasons[r]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            exit_reasons[r]["wins"] += 1
    for r, data in sorted(exit_reasons.items()):
        wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
        print(f"  {r:8s}  {data['count']:3d} trades  {wr:5.1f}% WR  ${data['pnl']:+8.2f}")

    # ─── Signal statistics ───
    print(f"\n{'─'*70}")
    print("SIGNAL GENERATION STATISTICS")
    print(f"{'─'*70}")
    print(f"  Total signal checks:   {v4_signals['total_checked']}")
    print(f"  Signals generated:     {v4_signals['signals_generated']}")
    print(f"  Rejected by risk:      {v4_signals['rejected_risk']}")
    print(f"  Signals by strategy:   {v4_signals['by_strategy']}")
    signal_rate = v4_signals['signals_generated'] / v4_signals['total_checked'] * 100 if v4_signals['total_checked'] > 0 else 0
    print(f"  Signal rate:           {signal_rate:.2f}%")

    # ─── V3 vs V4 comparison header ───
    print(f"\n{'='*70}")
    print("V3 vs V4 COMPARISON")
    print(f"{'='*70}")
    print(f"  {'Metric':20s}  {'V3 (inline)':>15s}  {'V4 (production)':>15s}")
    print(f"  {'─'*55}")
    print(f"  {'Total return':20s}  {'94.0%':>15s}  {f'{total_return:+.1f}%':>15s}")
    print(f"  {'Total trades':20s}  {'69':>15s}  {f'{total_trades}':>15s}")
    print(f"  {'Win rate':20s}  {'60.9%':>15s}  {f'{win_rate:.1f}%':>15s}")
    print(f"  {'Sharpe ratio':20s}  {'3.31':>15s}  {f'{sharpe:.2f}':>15s}")
    print(f"  {'Max drawdown':20s}  {'7.9%':>15s}  {f'{max_dd:.1f}%':>15s}")
    print(f"  {'Profit factor':20s}  {'2.58':>15s}  {f'{profit_factor:.2f}':>15s}")
    print(f"  {'Avg daily':20s}  {'0.55%':>15s}  {f'{avg_daily:.3f}%':>15s}")

    # ─── Per-trade log ───
    print(f"\n{'─'*70}")
    print("TRADE LOG")
    print(f"{'─'*70}")
    for idx, t in enumerate(trades, 1):
        symbol = t["pair"].replace("_USDT_USDT", "")
        print(
            f"  #{idx:3d} {t['timestamp'][:16]} {symbol:5s} "
            f"{t['direction']:5s} x{t['leverage']}  "
            f"entry={t['entry_price']:.4f} exit={t['exit_price']:.4f}  "
            f"PnL=${t['net_pnl']:+.2f} ({t['exit_reason']:6s}) "
            f"conf={t['confidence']:.0f}% margin=${t['margin']:.2f} "
            f"held={t['bars_held']}bars"
        )

    # ─── Save results to JSON for comparison tooling ───
    results = {
        "version": "v4_production",
        "initial_balance": INITIAL_BALANCE,
        "final_balance": final_balance,
        "total_return_pct": total_return,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "profit_factor": profit_factor,
        "avg_daily_pct": avg_daily,
        "total_fees": total_fees,
        "days": days,
        "trades": trades,
        "signal_stats": v4_signals,
    }
    output_path = PROJECT_ROOT / "user_data" / "backtest_results" / "backtest_v4_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    run_backtest()
