"""
Backtesting engine for Claude Quant strategies.

Runs walk-forward backtest on historical data:
- 4H for regime detection + strategy selection
- 1H for entry signal generation
- Full risk management (Kelly, CB, GARCH, fees)
- Reports: Sharpe, win rate, max drawdown, total return
"""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.indicator_engine import IndicatorEngine
from src.data.data_validator import DataValidator
from src.strategies.regime_detector import RegimeDetector
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.base_strategy import SignalDirection
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel
from src.risk.position_sizer import PositionSizer
from src.risk.leverage_manager import LeverageManager
from src.risk.volatility_model import VolatilityModel
from src.execution.fee_calculator import FeeCalculator

DATA_DIR = PROJECT_ROOT / "user_data" / "data"

PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]
INITIAL_BALANCE = 68.33
HARD_FLOOR = 30.0


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    """Load historical OHLCV data."""
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def run_backtest():
    """Run full backtest across all pairs."""
    indicator_engine = IndicatorEngine()
    data_validator = DataValidator()
    regime_detector = RegimeDetector()
    adaptive_strategy = AdaptiveStrategy()
    position_sizer = PositionSizer()
    fee_calculator = FeeCalculator()
    volatility_model = VolatilityModel(forecast_horizon=1)

    # Load all data
    data_1h = {}
    data_4h = {}
    for pair in PAIRS:
        data_1h[pair] = load_data(pair, "1h")
        data_4h[pair] = load_data(pair, "4h")

    # Simulation state
    balance = INITIAL_BALANCE
    peak_balance = balance
    daily_start = balance
    trades = []
    equity_curve = []
    open_positions = []  # Support multiple concurrent positions (up to CB max)

    # Walk forward: iterate through 1H candles (skip first 200 for indicator warmup)
    # Use 4H regime from the corresponding 4H candle
    min_1h_len = min(len(data_1h[p]) for p in PAIRS)
    start_idx = 200  # Warmup period

    print(f"Backtesting from index {start_idx} to {min_1h_len}")
    print(f"Initial balance: ${balance:.2f}")
    print(f"{'='*60}")

    daily_pnl = {}

    for i in range(start_idx, min_1h_len):
        # Check hard floor
        if balance < HARD_FLOOR:
            print(f"\nHARD FLOOR HIT at ${balance:.2f} — stopping")
            break

        # Current timestamp for daily tracking
        ts = data_1h[PAIRS[0]]["timestamp"].iloc[i]
        day = ts.strftime("%Y-%m-%d")

        # Track daily start
        if day not in daily_pnl:
            daily_pnl[day] = {"start": balance, "end": balance, "trades": 0}

        # Manage open positions — trailing SL + TP + time exit
        closed_indices = []
        for pidx, pos in enumerate(open_positions):
            pair = pos["pair"]
            current_high = data_1h[pair]["high"].iloc[i]
            current_low = data_1h[pair]["low"].iloc[i]
            current_close = data_1h[pair]["close"].iloc[i]

            # Trailing stop: activate after 1.0 ATR favorable, trail at 1.5 ATR
            # This gives winners room to breathe while locking profit
            atr_val = pos.get("atr", abs(pos["take_profit"] - pos["entry_price"]) / 4.0)
            if pos["direction"] == "long":
                if current_high > pos.get("best_price", pos["entry_price"]):
                    pos["best_price"] = current_high
                favorable_move = pos["best_price"] - pos["entry_price"]
                if favorable_move > 1.0 * atr_val:
                    new_sl = pos["best_price"] - 1.5 * atr_val
                    if new_sl > pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
            else:
                if current_low < pos.get("best_price", pos["entry_price"]):
                    pos["best_price"] = current_low
                favorable_move = pos["entry_price"] - pos["best_price"]
                if favorable_move > 1.0 * atr_val:
                    new_sl = pos["best_price"] + 1.5 * atr_val
                    if new_sl < pos["stop_loss"]:
                        pos["stop_loss"] = new_sl

            hit_sl = False
            hit_tp = False
            hit_time = False
            exit_price = None

            if pos["direction"] == "long":
                if current_low <= pos["stop_loss"]:
                    hit_sl = True
                    exit_price = pos["stop_loss"]
                elif current_high >= pos["take_profit"]:
                    hit_tp = True
                    exit_price = pos["take_profit"]
            else:
                if current_high >= pos["stop_loss"]:
                    hit_sl = True
                    exit_price = pos["stop_loss"]
                elif current_low <= pos["take_profit"]:
                    hit_tp = True
                    exit_price = pos["take_profit"]

            # Time-based exit: close after 72 hours (3 days) if no SL/TP hit
            bars_held = i - pos["entry_idx"]
            if not hit_sl and not hit_tp and bars_held >= 72:
                hit_time = True
                exit_price = current_close

            if hit_sl or hit_tp or hit_time:
                if pos["direction"] == "long":
                    raw_pnl = (exit_price - pos["entry_price"]) * pos["size"]
                else:
                    raw_pnl = (pos["entry_price"] - exit_price) * pos["size"]

                entry_notional = pos["entry_price"] * pos["size"]
                exit_notional = exit_price * pos["size"]
                entry_fee = float(fee_calculator.calculate_fees(
                    Decimal(str(entry_notional)), is_maker=False
                ))
                exit_fee = float(fee_calculator.calculate_fees(
                    Decimal(str(exit_notional)), is_maker=False
                ))
                total_fees = entry_fee + exit_fee
                net_pnl = raw_pnl - total_fees

                balance += net_pnl
                peak_balance = max(peak_balance, balance)

                trade_record = {
                    "pair": pair,
                    "direction": pos["direction"],
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "size": pos["size"],
                    "leverage": pos["leverage"],
                    "raw_pnl": raw_pnl,
                    "fees": total_fees,
                    "net_pnl": net_pnl,
                    "margin": pos["margin"],
                    "roi_pct": (net_pnl / pos["margin"]) * 100 if pos["margin"] > 0 else 0,
                    "is_win": net_pnl > 0,
                    "exit_reason": "TP" if hit_tp else ("TIME" if hit_time else "SL"),
                    "strategy": pos["strategy"],
                    "regime": pos["regime"],
                    "timestamp": str(ts),
                }
                trades.append(trade_record)
                daily_pnl[day]["trades"] += 1
                closed_indices.append(pidx)

        # Remove closed positions (reverse order to preserve indices)
        for pidx in sorted(closed_indices, reverse=True):
            open_positions.pop(pidx)

        # Circuit breaker check
        cb_level = CircuitBreaker.check_level(Decimal(str(balance)))
        if cb_level == CircuitBreakerLevel.DEAD:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        constraints = CircuitBreaker.get_constraints(Decimal(str(balance)))

        # Check if at max positions
        if len(open_positions) >= constraints.max_positions:
            equity_curve.append({"timestamp": str(ts), "balance": balance})
            daily_pnl[day]["end"] = balance
            continue

        # Don't open another position in a pair we already have open
        open_pairs = {p["pair"] for p in open_positions}

        # Open positions on ALL available pairs (multi-position)
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
                df_4h_ind = indicator_engine.calculate_all(df_4h.tail(200).copy())
                df_1h_ind = indicator_engine.calculate_all(df_1h.tail(200).copy())
            except Exception:
                continue

            try:
                regime = regime_detector.detect(df_4h_ind)
            except Exception:
                continue

            strategy = adaptive_strategy.select_strategy(regime)
            if strategy is None:
                continue

            try:
                signal = strategy.generate_signal(df_1h_ind)
            except Exception as exc:
                if strategy.name != "TrendFollower" and i < 220:
                    print(f"  DEBUG [{pair}] {strategy.name} exception: {exc}")
                continue

            if signal.direction == SignalDirection.NONE:
                if strategy.name != "TrendFollower" and i % 100 == 0:
                    print(f"  DEBUG [{pair}] {strategy.name} NONE: {signal.reasoning[:80]}")
                continue
            if signal.confidence < AdaptiveStrategy.MIN_CONFIDENCE:
                if strategy.name != "TrendFollower":
                    print(f"  DEBUG [{pair}] {strategy.name} LOW CONF: {signal.confidence:.1f}% < {AdaptiveStrategy.MIN_CONFIDENCE}%")
                continue

            try:
                leverage_result = LeverageManager.determine_leverage(
                    confidence=signal.confidence,
                    regime=signal.regime,
                    circuit_breaker_level=cb_level,
                )
                leverage = leverage_result.leverage
                if leverage == 0:
                    if strategy.name != "TrendFollower":
                        print(f"  DEBUG [{pair}] {strategy.name} LEVERAGE=0")
                    continue

                df_for_garch = data_1h[pair].iloc[:i + 1].tail(500).copy()
                vol_state = volatility_model.forecast_simple(df_for_garch)
                leverage = VolatilityModel.adjust_leverage(
                    requested_leverage=leverage,
                    vol_state=vol_state,
                    max_leverage=constraints.max_leverage,
                )
                if leverage == 0:
                    if strategy.name != "TrendFollower":
                        print(f"  DEBUG [{pair}] {strategy.name} GARCH_LEV=0")
                    continue

                win_rate = 0.5
                if len(trades) >= 20:
                    recent = trades[-50:]
                    win_rate = sum(1 for t in recent if t["is_win"]) / len(recent)

                rr_ratio = 2.0
                kelly = (win_rate * rr_ratio - (1 - win_rate)) / rr_ratio
                half_kelly = max(0, kelly * 0.5)
                position_pct = min(half_kelly, 0.15)
                position_pct *= float(constraints.size_multiplier)

                margin = balance * position_pct
                notional = margin * leverage

                # Binance min notional: ETH=$20, SOL/DOGE=$5
                min_notional = 20.0 if "ETH" in pair else 5.0
                if notional < min_notional:
                    if strategy.name != "TrendFollower":
                        print(f"  DEBUG [{pair}] {strategy.name} NOTIONAL_TOO_SMALL: ${notional:.2f} < ${min_notional}")
                    continue
                size = notional / signal.entry_price
                atr_val = abs(signal.entry_price - signal.stop_loss) / 2.0

                if strategy.name != "TrendFollower":
                    print(f"  >>> ENTRY [{pair}] {strategy.name} {signal.direction.value} "
                          f"@ {signal.entry_price:.4f}, conf={signal.confidence:.1f}%, lev={leverage}x, margin=${margin:.2f}")

                open_positions.append({
                    "pair": pair,
                    "direction": signal.direction.value,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "size": size,
                    "leverage": leverage,
                    "margin": margin,
                    "strategy": signal.strategy_name,
                    "regime": regime.regime.value,
                    "entry_idx": i,
                    "best_price": signal.entry_price,
                    "atr": atr_val,
                })
            except Exception as exc:
                if strategy.name != "TrendFollower":
                    print(f"  DEBUG [{pair}] {strategy.name} EXCEPTION in sizing: {exc}")
                continue

        equity_curve.append({"timestamp": str(ts), "balance": balance})
        daily_pnl[day]["end"] = balance

    # ─── Results ───
    print(f"\n{'='*60}")
    print("BACKTEST RESULTS")
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

    # Max drawdown
    eq = [INITIAL_BALANCE] + [e["balance"] for e in equity_curve]
    peak = np.maximum.accumulate(eq)
    drawdown = (peak - eq) / peak * 100
    max_dd = np.max(drawdown)

    # Daily returns for Sharpe
    daily_returns = []
    for day, dpnl in sorted(daily_pnl.items()):
        if dpnl["start"] > 0:
            daily_ret = (dpnl["end"] - dpnl["start"]) / dpnl["start"]
            daily_returns.append(daily_ret)

    daily_returns = np.array(daily_returns)
    sharpe = 0.0
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(365)

    # Average daily return
    avg_daily_return = np.mean(daily_returns) * 100 if len(daily_returns) > 0 else 0

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
    print(f"Profit Factor:   {(avg_win * wins) / (avg_loss * losses):.2f}" if losses > 0 and avg_loss > 0 else "Profit Factor:   N/A")
    print()
    print(f"Max Drawdown:    {max_dd:.1f}%")
    print(f"Sharpe Ratio:    {sharpe:.2f}")
    print(f"Avg Daily Return:{avg_daily_return:+.3f}%")
    print()

    # Strategy breakdown
    print("Strategy Breakdown:")
    strategy_stats = {}
    for t in trades:
        s = t["strategy"]
        if s not in strategy_stats:
            strategy_stats[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
        strategy_stats[s]["trades"] += 1
        strategy_stats[s]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            strategy_stats[s]["wins"] += 1

    for s, stats in sorted(strategy_stats.items()):
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
        print(f"  {s:20s}: {stats['trades']:3d} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")

    # Regime breakdown
    print("\nRegime Breakdown:")
    regime_stats = {}
    for t in trades:
        r = t["regime"]
        if r not in regime_stats:
            regime_stats[r] = {"trades": 0, "wins": 0, "pnl": 0.0}
        regime_stats[r]["trades"] += 1
        regime_stats[r]["pnl"] += t["net_pnl"]
        if t["is_win"]:
            regime_stats[r]["wins"] += 1

    for r, stats in sorted(regime_stats.items()):
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
        print(f"  {r:20s}: {stats['trades']:3d} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")

    # Save results
    results = {
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
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe_ratio": round(sharpe, 2),
        "avg_daily_return_pct": round(avg_daily_return, 3),
        "strategy_breakdown": strategy_stats,
        "regime_breakdown": regime_stats,
        "trades": trades,
    }

    results_dir = PROJECT_ROOT / "user_data" / "backtest_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to: {results_file}")

    # Pass/fail criteria
    print(f"\n{'='*60}")
    print("PASS/FAIL CRITERIA:")
    checks = [
        ("Sharpe > 1.0", sharpe > 1.0),
        ("Max DD < 40%", max_dd < 40),
        ("Win Rate > 45%", win_rate > 45),
        ("Trades > 50", total_trades > 50),
        ("Positive Total P&L", total_pnl > 0),
        ("Avg Daily > 0%", avg_daily_return > 0),
    ]
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")


if __name__ == "__main__":
    run_backtest()
