from __future__ import annotations

import pandas as pd

from src.strategies.base_strategy import SignalDirection
from src.strategies.scalper import Scalper


def test_scalper_adjusts_take_profit_for_fees(monkeypatch):
    strategy = Scalper()
    called = {}

    monkeypatch.setattr(strategy, "_validate", lambda df: None)
    monkeypatch.setattr(strategy, "_is_volatile", lambda df, atr: False)
    monkeypatch.setattr(strategy, "_detect_bullish_divergence", lambda df: (True, 80.0))
    monkeypatch.setattr(strategy, "_detect_bearish_divergence", lambda df: (False, 0.0))
    monkeypatch.setattr(strategy, "_volatility_factor", lambda df, atr: 0.0)
    monkeypatch.setattr(strategy, "_compute_confidence", lambda **kwargs: 70.0)
    monkeypatch.setattr(strategy, "_build_reasoning", lambda *args, **kwargs: "fee adjusted scalp")

    def fake_adjust_tp_for_fees(**kwargs):
        called.update(kwargs)
        return kwargs["raw_tp"] + 1

    class _FakeFeeCalc:
        def adjust_tp_for_fees(self, **kwargs):
            return fake_adjust_tp_for_fees(**kwargs)

    monkeypatch.setattr(strategy, "_fee_calculator", _FakeFeeCalc())

    df = pd.DataFrame(
        {
            "close": [100.0, 100.0, 100.0],
            "rsi": [35.0, 36.0, 37.0],
            "adx": [30.0, 30.0, 30.0],
            "atr": [1.0, 1.0, 1.0],
            "ema_9": [101.0, 101.0, 101.0],
            "ema_21": [100.0, 100.0, 100.0],
            "ema_50": [99.0, 99.0, 99.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "high": [101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 99.0],
        }
    )

    signal = strategy.generate_signal(df)

    assert signal.direction == SignalDirection.LONG
    assert called["is_long"] is True
    assert signal.take_profit == round(float(called["raw_tp"] + 1), 8)