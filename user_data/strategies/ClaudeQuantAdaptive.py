"""
ClaudeQuantAdaptive - Master Freqtrade Strategy

Bridges Claude agent decisions to Freqtrade execution engine.
Reads agent decisions from JSON state files in user_data/agent_state/.

The Claude agent system writes decision files, and this strategy
reads them to execute trades through Freqtrade's infrastructure.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import talib

from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter
from freqtrade.persistence import Trade

logger = logging.getLogger(__name__)

AGENT_STATE_DIR = Path(__file__).parent.parent / "agent_state"


class ClaudeQuantAdaptive(IStrategy):
    """
    Master strategy that delegates to Claude agent system.

    Reads agent decisions from JSON files and applies them through
    Freqtrade's callback system. Also provides standalone indicator-based
    signals as fallback when agent system is unavailable.
    """

    # Strategy parameters
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True

    # Minimal ROI - managed by agent's take-profit logic
    minimal_roi = {
        "0": 0.15,    # 15% max if TP not hit
        "60": 0.10,   # 10% after 1 hour
        "120": 0.05,  # 5% after 2 hours
        "240": 0.02,  # 2% after 4 hours
    }

    # Stoploss - overridden by custom_stoploss callback
    stoploss = -0.05  # 5% hard floor, custom_stoploss handles the rest

    # Trailing stop
    trailing_stop = False  # Managed by custom_stoploss

    # Order types
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
    }

    # Hyperopt parameters
    buy_adx_threshold = IntParameter(20, 35, default=25, space="buy")
    buy_rsi_lower = IntParameter(25, 40, default=30, space="buy")
    sell_rsi_upper = IntParameter(60, 80, default=70, space="sell")
    atr_sl_multiplier = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space="stoploss")

    def informative_pairs(self) -> list:
        """Additional pairs for correlation/confirmation."""
        return [
            ("BTC/USDT:USDT", "15m"),
            ("BTC/USDT:USDT", "1h"),
        ]

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Calculate all technical indicators."""
        # EMAs
        for period in [9, 21, 50, 200]:
            dataframe[f"ema_{period}"] = talib.EMA(dataframe["close"], timeperiod=period)

        # RSI
        dataframe["rsi"] = talib.RSI(dataframe["close"], timeperiod=14)

        # MACD
        macd, signal, hist = talib.MACD(
            dataframe["close"], fastperiod=12, slowperiod=26, signalperiod=9
        )
        dataframe["macd"] = macd
        dataframe["macd_signal"] = signal
        dataframe["macd_hist"] = hist

        # ADX
        dataframe["adx"] = talib.ADX(
            dataframe["high"], dataframe["low"], dataframe["close"], timeperiod=14
        )
        dataframe["plus_di"] = talib.PLUS_DI(
            dataframe["high"], dataframe["low"], dataframe["close"], timeperiod=14
        )
        dataframe["minus_di"] = talib.MINUS_DI(
            dataframe["high"], dataframe["low"], dataframe["close"], timeperiod=14
        )

        # Bollinger Bands
        upper, middle, lower = talib.BBANDS(
            dataframe["close"], timeperiod=20, nbdevup=2, nbdevdn=2
        )
        dataframe["bb_upper"] = upper
        dataframe["bb_middle"] = middle
        dataframe["bb_lower"] = lower
        dataframe["bb_width"] = (upper - lower) / middle

        # ATR
        dataframe["atr"] = talib.ATR(
            dataframe["high"], dataframe["low"], dataframe["close"], timeperiod=14
        )

        # Supertrend
        dataframe = self._calculate_supertrend(dataframe, period=10, multiplier=3)

        # Volume SMA
        dataframe["volume_sma"] = talib.SMA(dataframe["volume"], timeperiod=20)
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_sma"]

        # Z-score for mean reversion
        close_mean = dataframe["close"].rolling(20).mean()
        close_std = dataframe["close"].rolling(20).std()
        dataframe["zscore"] = (dataframe["close"] - close_mean) / close_std

        # Regime detection
        dataframe["regime"] = self._detect_regime(dataframe)

        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Generate entry signals based on regime and agent decisions."""
        # Check for agent decision file
        agent_signal = self._read_agent_signal(metadata["pair"])

        if agent_signal and agent_signal.get("action") == "enter":
            if agent_signal.get("direction") == "long":
                dataframe.loc[dataframe.index[-1], "enter_long"] = 1
                dataframe.loc[dataframe.index[-1], "enter_tag"] = agent_signal.get(
                    "strategy", "agent"
                )
            elif agent_signal.get("direction") == "short":
                dataframe.loc[dataframe.index[-1], "enter_short"] = 1
                dataframe.loc[dataframe.index[-1], "enter_tag"] = agent_signal.get(
                    "strategy", "agent"
                )
            return dataframe

        # Fallback: standalone indicator signals
        # Long: Trend following
        dataframe.loc[
            (
                (dataframe["ema_9"] > dataframe["ema_21"])
                & (dataframe["adx"] > self.buy_adx_threshold.value)
                & (dataframe["rsi"] > self.buy_rsi_lower.value)
                & (dataframe["rsi"] < self.sell_rsi_upper.value)
                & (dataframe["supertrend_direction"] == 1)
                & (dataframe["volume_ratio"] > 0.8)
            ),
            "enter_long",
        ] = 1

        # Short: Trend following (reversed)
        dataframe.loc[
            (
                (dataframe["ema_9"] < dataframe["ema_21"])
                & (dataframe["adx"] > self.buy_adx_threshold.value)
                & (dataframe["rsi"] < self.sell_rsi_upper.value)
                & (dataframe["rsi"] > self.buy_rsi_lower.value)
                & (dataframe["supertrend_direction"] == -1)
                & (dataframe["volume_ratio"] > 0.8)
            ),
            "enter_short",
        ] = 1

        # Long: Mean reversion
        dataframe.loc[
            (
                (dataframe["close"] <= dataframe["bb_lower"])
                & (dataframe["rsi"] < 30)
                & (dataframe["adx"] < 20)
                & (dataframe["zscore"] < -2)
            ),
            "enter_long",
        ] = 1

        # Short: Mean reversion
        dataframe.loc[
            (
                (dataframe["close"] >= dataframe["bb_upper"])
                & (dataframe["rsi"] > 70)
                & (dataframe["adx"] < 20)
                & (dataframe["zscore"] > 2)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Generate exit signals."""
        agent_signal = self._read_agent_signal(metadata["pair"])

        if agent_signal and agent_signal.get("action") == "exit":
            if agent_signal.get("direction") == "long":
                dataframe.loc[dataframe.index[-1], "exit_long"] = 1
            elif agent_signal.get("direction") == "short":
                dataframe.loc[dataframe.index[-1], "exit_short"] = 1
            return dataframe

        # Exit long on EMA cross down or RSI overbought
        dataframe.loc[
            (
                (dataframe["ema_9"] < dataframe["ema_21"])
                | (dataframe["rsi"] > 75)
            ),
            "exit_long",
        ] = 1

        # Exit short on EMA cross up or RSI oversold
        dataframe.loc[
            (
                (dataframe["ema_9"] > dataframe["ema_21"])
                | (dataframe["rsi"] < 25)
            ),
            "exit_short",
        ] = 1

        return dataframe

    def leverage(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
        side: str, **kwargs
    ) -> float:
        """Dynamic leverage from agent decision."""
        agent_signal = self._read_agent_signal(pair)
        if agent_signal and "leverage" in agent_signal:
            requested = agent_signal["leverage"]
            # Cap at max allowed
            return min(float(requested), float(max_leverage), 10.0)

        # Default conservative leverage based on ADX
        return 3.0

    def custom_stake_amount(
        self, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float],
        max_stake: float, leverage: float, entry_tag: Optional[str],
        side: str, **kwargs
    ) -> float:
        """Dynamic position sizing from agent decision."""
        agent_signal = self._read_agent_signal(kwargs.get("pair", ""))
        if agent_signal and "stake_amount" in agent_signal:
            requested = float(agent_signal["stake_amount"])
            # Enforce bounds
            if min_stake and requested < min_stake:
                return min_stake
            return min(requested, max_stake)

        # Default: use proposed stake
        return proposed_stake

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: Optional[str],
        side: str, **kwargs
    ) -> bool:
        """Final confirmation before trade entry."""
        # Always require agent approval for non-fallback trades
        agent_signal = self._read_agent_signal(pair)

        if agent_signal:
            # Agent explicitly approved
            if agent_signal.get("approved", False):
                logger.info(
                    f"Agent approved trade: {pair} {side} @ {rate} "
                    f"strategy={agent_signal.get('strategy', 'unknown')}"
                )
                return True
            # Agent explicitly rejected
            if agent_signal.get("rejected", False):
                logger.info(f"Agent rejected trade: {pair} {side}")
                return False

        # Fallback signals: allow if no agent decision exists
        return True

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, after_fill: bool,
        **kwargs
    ) -> float:
        """Dynamic stop-loss from agent decision."""
        agent_signal = self._read_agent_signal(pair)

        if agent_signal and "stop_loss_price" in agent_signal:
            sl_price = float(agent_signal["stop_loss_price"])
            if trade.is_short:
                # For shorts, SL is above entry
                sl_pct = (sl_price - trade.open_rate) / trade.open_rate
            else:
                # For longs, SL is below entry
                sl_pct = (trade.open_rate - sl_price) / trade.open_rate
            return -abs(sl_pct)

        # Default: ATR-based stop loss
        # Use the stoploss parameter as fallback
        return self.stoploss

    # ─── Helper Methods ───

    def _read_agent_signal(self, pair: str) -> Optional[dict]:
        """Read the latest agent decision for a pair."""
        if not pair:
            return None
        safe_pair = pair.replace("/", "_").replace(":", "_")
        signal_file = AGENT_STATE_DIR / f"signal_{safe_pair}.json"

        if not signal_file.exists():
            return None

        try:
            data = json.loads(signal_file.read_text())
            # Check freshness (must be < 10 minutes old)
            ts = datetime.fromisoformat(data.get("timestamp", "2000-01-01"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > 600:
                logger.debug(f"Stale agent signal for {pair} ({age:.0f}s old)")
                return None
            return data
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to read agent signal for {pair}: {e}")
            return None

    def _calculate_supertrend(
        self, df: pd.DataFrame, period: int = 10, multiplier: int = 3
    ) -> pd.DataFrame:
        """Calculate Supertrend indicator."""
        atr = talib.ATR(df["high"], df["low"], df["close"], timeperiod=period)
        hl2 = (df["high"] + df["low"]) / 2

        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)

        supertrend = pd.Series(index=df.index, dtype=float)
        direction = pd.Series(index=df.index, dtype=int)

        supertrend.iloc[0] = upper_band.iloc[0]
        direction.iloc[0] = 1

        for i in range(1, len(df)):
            if df["close"].iloc[i] > upper_band.iloc[i - 1]:
                direction.iloc[i] = 1
            elif df["close"].iloc[i] < lower_band.iloc[i - 1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1:
                supertrend.iloc[i] = max(lower_band.iloc[i], supertrend.iloc[i - 1]) \
                    if direction.iloc[i - 1] == 1 else lower_band.iloc[i]
            else:
                supertrend.iloc[i] = min(upper_band.iloc[i], supertrend.iloc[i - 1]) \
                    if direction.iloc[i - 1] == -1 else upper_band.iloc[i]

        df["supertrend"] = supertrend
        df["supertrend_direction"] = direction
        return df

    def _detect_regime(self, df: pd.DataFrame) -> pd.Series:
        """Simple regime detection for indicator-based signals."""
        regime = pd.Series("quiet", index=df.index)

        # Trending
        trending = (df["adx"] > 25)
        regime[trending] = "trending"

        # Ranging
        ranging = (df["adx"] < 20)
        regime[ranging] = "ranging"

        # Volatile - overrides if BB width is extreme
        bb_width_avg = df["bb_width"].rolling(100).mean()
        volatile = (df["bb_width"] > 1.5 * bb_width_avg) & (df["adx"] >= 15)
        regime[volatile] = "volatile"

        # Quiet - low everything
        quiet = (df["adx"] < 15) & (df["volume_ratio"] < 0.5)
        regime[quiet] = "quiet"

        return regime
