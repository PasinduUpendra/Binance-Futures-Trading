"""
Diagnostic: count why MeanReversion and BreakoutTrader reject signals.
Runs through backtest data and tallies rejection reasons.
"""

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.indicator_engine import IndicatorEngine
from src.strategies.regime_detector import RegimeDetector, MarketRegime
from src.strategies.mean_reversion import MeanReversion
from src.strategies.breakout_trader import BreakoutTrader
from src.strategies.adaptive_strategy import AdaptiveStrategy

DATA_DIR = PROJECT_ROOT / "user_data" / "data"
PAIRS = ["ETH_USDT_USDT", "SOL_USDT_USDT", "DOGE_USDT_USDT"]


def load_data(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{timeframe}.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def main():
    ie = IndicatorEngine()
    rd = RegimeDetector()
    mr = MeanReversion()
    bo = BreakoutTrader()

    regime_counts = Counter()
    mr_rejection_reasons = Counter()
    bo_rejection_reasons = Counter()
    mr_indicator_stats = {"adx": [], "zscore": [], "rsi": [], "close_vs_bb": []}

    # Track when regime says ranging but MR fails
    ranging_mr_attempts = 0
    ranging_mr_successes = 0

    for pair in PAIRS:
        data_1h = load_data(pair, "1h")
        data_4h = load_data(pair, "4h")
        min_len = len(data_1h)

        for i in range(200, min_len, 4):  # Sample every 4th candle for speed
            df_1h = data_1h.iloc[:i + 1].copy()
            current_ts = df_1h["timestamp"].iloc[-1]
            df_4h = data_4h[data_4h["timestamp"] <= current_ts].copy()

            if len(df_4h) < 100:
                continue

            try:
                df_4h_ind = ie.calculate_all(df_4h.tail(200).copy())
                df_1h_ind = ie.calculate_all(df_1h.tail(200).copy())
            except Exception:
                continue

            try:
                regime = rd.detect(df_4h_ind)
            except Exception:
                continue

            regime_counts[regime.regime.value] += 1

            # When regime is RANGING, try MR and track why it fails
            if regime.regime in (MarketRegime.RANGING, MarketRegime.QUIET):
                ranging_mr_attempts += 1
                signal = mr.generate_signal(df_1h_ind)
                if signal.direction.value != "none":
                    ranging_mr_successes += 1
                else:
                    mr_rejection_reasons[signal.reasoning] += 1

                # Collect indicator values for ranging regimes
                adx_1h = float(df_1h_ind["adx"].dropna().iloc[-1]) if "adx" in df_1h_ind.columns else np.nan
                zscore_1h = float(df_1h_ind["zscore"].dropna().iloc[-1]) if "zscore" in df_1h_ind.columns else np.nan
                rsi_1h = float(df_1h_ind["rsi"].dropna().iloc[-1]) if "rsi" in df_1h_ind.columns else np.nan
                close_1h = float(df_1h_ind["close"].iloc[-1])
                bb_lower = float(df_1h_ind["bb_lower"].dropna().iloc[-1]) if "bb_lower" in df_1h_ind.columns else np.nan
                bb_upper = float(df_1h_ind["bb_upper"].dropna().iloc[-1]) if "bb_upper" in df_1h_ind.columns else np.nan

                mr_indicator_stats["adx"].append(adx_1h)
                mr_indicator_stats["zscore"].append(zscore_1h)
                mr_indicator_stats["rsi"].append(rsi_1h)
                if not np.isnan(bb_lower) and not np.isnan(bb_upper):
                    # How far is close from nearest BB band (negative = outside)
                    dist_lower = (close_1h - bb_lower) / close_1h * 100
                    dist_upper = (bb_upper - close_1h) / close_1h * 100
                    mr_indicator_stats["close_vs_bb"].append(min(dist_lower, dist_upper))

            # When regime is VOLATILE, try BO and track failures
            if regime.regime == MarketRegime.VOLATILE:
                signal = bo.generate_signal(df_1h_ind)
                if signal.direction.value == "none":
                    bo_rejection_reasons[signal.reasoning[:80]] += 1

    print("=" * 70)
    print("REGIME DISTRIBUTION (4H)")
    print("=" * 70)
    total = sum(regime_counts.values())
    for regime, count in regime_counts.most_common():
        print(f"  {regime:12s}: {count:5d} ({count/total*100:.1f}%)")

    print(f"\n{'=' * 70}")
    print("MEAN REVERSION DIAGNOSTICS (when regime=RANGING/QUIET)")
    print(f"{'=' * 70}")
    print(f"  Attempts: {ranging_mr_attempts}")
    print(f"  Successes: {ranging_mr_successes}")
    print(f"  Rejection Rate: {(1 - ranging_mr_successes/max(1,ranging_mr_attempts))*100:.1f}%")

    print(f"\n  Top rejection reasons:")
    for reason, count in mr_rejection_reasons.most_common(10):
        print(f"    [{count:4d}] {reason[:100]}")

    if mr_indicator_stats["adx"]:
        adx_arr = np.array(mr_indicator_stats["adx"])
        zscore_arr = np.array(mr_indicator_stats["zscore"])
        rsi_arr = np.array(mr_indicator_stats["rsi"])
        bb_arr = np.array(mr_indicator_stats["close_vs_bb"])

        print(f"\n  1H Indicator Distribution during RANGING regime:")
        print(f"    ADX     - mean: {np.nanmean(adx_arr):.1f}, median: {np.nanmedian(adx_arr):.1f}, "
              f"< 25: {(adx_arr < 25).sum()}/{len(adx_arr)} ({(adx_arr < 25).sum()/len(adx_arr)*100:.0f}%)")
        print(f"    Z-score - mean: {np.nanmean(zscore_arr):.2f}, std: {np.nanstd(zscore_arr):.2f}, "
              f"|z|>2: {(np.abs(zscore_arr)>2).sum()}/{len(zscore_arr)} ({(np.abs(zscore_arr)>2).sum()/len(zscore_arr)*100:.1f}%), "
              f"|z|>1.5: {(np.abs(zscore_arr)>1.5).sum()}/{len(zscore_arr)} ({(np.abs(zscore_arr)>1.5).sum()/len(zscore_arr)*100:.1f}%)")
        print(f"    RSI     - mean: {np.nanmean(rsi_arr):.1f}, "
              f"<30 or >70: {((rsi_arr<30)|(rsi_arr>70)).sum()}/{len(rsi_arr)} ({((rsi_arr<30)|(rsi_arr>70)).sum()/len(rsi_arr)*100:.1f}%), "
              f"<35 or >65: {((rsi_arr<35)|(rsi_arr>65)).sum()}/{len(rsi_arr)} ({((rsi_arr<35)|(rsi_arr>65)).sum()/len(rsi_arr)*100:.1f}%)")
        print(f"    BB dist - mean: {np.nanmean(bb_arr):.2f}%, "
              f"close at/below BB: {(np.array(bb_arr)<=0).sum()}/{len(bb_arr)} ({(np.array(bb_arr)<=0).sum()/len(bb_arr)*100:.1f}%)")

        # Combined conditions
        combined_z15_rsi35 = ((np.abs(zscore_arr)>1.5) & ((rsi_arr<35)|(rsi_arr>65))).sum()
        combined_z20_rsi30 = ((np.abs(zscore_arr)>2.0) & ((rsi_arr<30)|(rsi_arr>70))).sum()
        combined_all_strict = ((np.abs(zscore_arr)>2.0) & ((rsi_arr<30)|(rsi_arr>70)) & (adx_arr<25)).sum()
        combined_all_relaxed = ((np.abs(zscore_arr)>1.5) & ((rsi_arr<35)|(rsi_arr>65)) & (adx_arr<25)).sum()

        print(f"\n  Combined condition frequency:")
        print(f"    |z|>2.0 AND RSI<30/70 AND ADX<25 (current):  {combined_all_strict}/{len(zscore_arr)} ({combined_all_strict/len(zscore_arr)*100:.2f}%)")
        print(f"    |z|>1.5 AND RSI<35/65 AND ADX<25 (relaxed):  {combined_all_relaxed}/{len(zscore_arr)} ({combined_all_relaxed/len(zscore_arr)*100:.2f}%)")
        print(f"    |z|>1.5 AND RSI<35/65 (no ADX gate):         {combined_z15_rsi35}/{len(zscore_arr)} ({combined_z15_rsi35/len(zscore_arr)*100:.2f}%)")

    print(f"\n{'=' * 70}")
    print("BREAKOUT TRADER DIAGNOSTICS (when regime=VOLATILE)")
    print(f"{'=' * 70}")
    if bo_rejection_reasons:
        for reason, count in bo_rejection_reasons.most_common(10):
            print(f"    [{count:4d}] {reason}")
    else:
        print("    No volatile regime candles found")


if __name__ == "__main__":
    main()
