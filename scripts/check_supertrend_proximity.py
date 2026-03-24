"""
Quick diagnostic: Check Supertrend proximity for each pair.
Shows how close price is to a potential flip for each of the 9 pairs.
"""

import asyncio
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_data import MarketDataClient
from src.data.indicator_engine import IndicatorEngine


async def main() -> None:
    ie = IndicatorEngine()
    md = MarketDataClient()

    await md.connect()
    try:
        pairs = [
            "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
            "DOGE/USDT:USDT", "XRP/USDT:USDT", "LINK/USDT:USDT",
            "AVAX/USDT:USDT", "SUI/USDT:USDT", "ADA/USDT:USDT",
        ]

        print(f"{'Pair':20s} {'Price':>10s} {'ST Line':>10s} {'Dir':>5s} {'Gap%':>8s} {'ADX':>6s} {'ATR':>10s}")
        print("-" * 80)

        for pair in pairs:
            try:
                candles = await md.fetch_ohlcv(pair, "4h", limit=200)
                df = pd.DataFrame(candles)
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)
                df = ie.calculate_all(df)
                last = df.iloc[-1]
                prev = df.iloc[-2]

                price = float(last["close"])
                st = float(last["supertrend"])
                direction = int(last["supertrend_direction"])
                prev_dir = int(prev["supertrend_direction"])
                adx = float(last["adx"])
                atr = float(last["atr"])

                gap_pct = abs(price - st) / price * 100
                dir_str = "BULL" if direction == 1 else "BEAR"

                flip_imminent = ""
                if prev_dir != direction:
                    flip_imminent = " *** JUST FLIPPED ***"
                elif gap_pct < 1.0:
                    flip_imminent = " **FLIP CLOSE**"

                print(
                    f"{pair:20s} {price:>10.4f} {st:>10.4f} {dir_str:>5s} "
                    f"{gap_pct:>7.2f}% {adx:>6.1f} {atr:>10.4f}{flip_imminent}"
                )
            except Exception as e:
                print(f"{pair:20s} ERROR: {e}")
    finally:
        await md.close()


if __name__ == "__main__":
    asyncio.run(main())
