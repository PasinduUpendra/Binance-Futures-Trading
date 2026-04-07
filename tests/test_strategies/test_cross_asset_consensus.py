"""
Unit tests for the CrossAssetConsensus module.

Covers:
- Per-pair directional computation (EMA fast vs slow)
- Consensus score calculation
- Alignment boost (pair agrees with majority)
- Divergence penalty (pair disagrees with majority)
- Minimum pairs threshold (< MIN_PAIRS → no adjustment)
- Consensus threshold (< CONSENSUS_THRESHOLD → no adjustment)
- Missing/insufficient data handling
- Edge cases (equal bulls/bears, single pair, all NaN)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.cross_asset_consensus import CrossAssetConsensus


# ---------------------------------------------------------------------------
# Helper: generate price DataFrames with controlled EMA direction
# ---------------------------------------------------------------------------


def _make_price_df(
    n: int = 100,
    close_start: float = 1000.0,
    close_end: float = 1100.0,
) -> pd.DataFrame:
    """Build a DataFrame with 'close' prices from start to end (linear)."""
    closes = np.linspace(close_start, close_end, n)
    return pd.DataFrame({"close": closes})


def _bullish_df(n: int = 100) -> pd.DataFrame:
    """Price going up → EMA_fast > EMA_slow → direction = +1."""
    return _make_price_df(n=n, close_start=900.0, close_end=1100.0)


def _bearish_df(n: int = 100) -> pd.DataFrame:
    """Price going down → EMA_fast < EMA_slow → direction = -1."""
    return _make_price_df(n=n, close_start=1100.0, close_end=900.0)


@pytest.fixture
def consensus() -> CrossAssetConsensus:
    return CrossAssetConsensus()


# ═══════════════════════════════════════════════════════════════════
# 1. Direction Computation
# ═══════════════════════════════════════════════════════════════════


class TestDirection:
    """Tests for _get_direction on individual pairs."""

    def test_bullish_direction(self, consensus: CrossAssetConsensus) -> None:
        """Rising prices → direction = +1."""
        df = _bullish_df()
        assert consensus._get_direction(df) == 1

    def test_bearish_direction(self, consensus: CrossAssetConsensus) -> None:
        """Falling prices → direction = -1."""
        df = _bearish_df()
        assert consensus._get_direction(df) == -1

    def test_insufficient_data_returns_none(self, consensus: CrossAssetConsensus) -> None:
        """Fewer rows than EMA_SLOW_SPAN + 5 → None."""
        df = _make_price_df(n=10)
        assert consensus._get_direction(df) is None

    def test_missing_close_column_returns_none(self, consensus: CrossAssetConsensus) -> None:
        """No 'close' column → None."""
        df = pd.DataFrame({"price": [100.0] * 100})
        assert consensus._get_direction(df) is None


# ═══════════════════════════════════════════════════════════════════
# 2. Consensus with Strong Agreement
# ═══════════════════════════════════════════════════════════════════


class TestStrongConsensus:
    """Tests when most pairs agree on direction."""

    def test_all_bullish_boosts_bullish(self, consensus: CrossAssetConsensus) -> None:
        """All pairs bullish → positive adjustments for all."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bullish_df(),
            "DOGE/USDT:USDT": _bullish_df(),
        }
        adj = consensus.compute(pair_data)
        for sym, val in adj.items():
            assert val > 0, f"{sym} should have positive adjustment, got {val}"

    def test_all_bearish_boosts_bearish(self, consensus: CrossAssetConsensus) -> None:
        """All pairs bearish → negative directions but consensus aligned → positive adj."""
        pair_data = {
            "BTC/USDT:USDT": _bearish_df(),
            "ETH/USDT:USDT": _bearish_df(),
            "SOL/USDT:USDT": _bearish_df(),
            "DOGE/USDT:USDT": _bearish_df(),
        }
        adj = consensus.compute(pair_data)
        # Every pair is aligned with the bearish consensus → positive adjustment
        for sym, val in adj.items():
            assert val > 0, f"{sym} aligned with consensus should be positive, got {val}"

    def test_majority_bullish_penalises_bearish(self, consensus: CrossAssetConsensus) -> None:
        """3 bullish + 1 bearish → bearish pair gets negative adjustment."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bullish_df(),
            "DOGE/USDT:USDT": _bearish_df(),  # Divergent
        }
        adj = consensus.compute(pair_data)
        # Consensus = (1+1+1-1)/4 = 0.5 → above threshold 0.3
        assert adj["BTC/USDT:USDT"] > 0
        assert adj["ETH/USDT:USDT"] > 0
        assert adj["SOL/USDT:USDT"] > 0
        assert adj["DOGE/USDT:USDT"] < 0  # Penalised for diverging


# ═══════════════════════════════════════════════════════════════════
# 3. Consensus Threshold
# ═══════════════════════════════════════════════════════════════════


class TestConsensusThreshold:
    """Tests for when consensus is too weak to apply adjustments."""

    def test_equal_splits_no_adjustment(self, consensus: CrossAssetConsensus) -> None:
        """2 bullish + 2 bearish → consensus = 0 → no adjustment."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bearish_df(),
            "DOGE/USDT:USDT": _bearish_df(),
        }
        adj = consensus.compute(pair_data)
        for sym, val in adj.items():
            assert val == 0.0, f"{sym} should be 0 when consensus is 0, got {val}"

    def test_below_threshold_no_adjustment(self, consensus: CrossAssetConsensus) -> None:
        """Consensus |score| < 0.3 → no adjustments."""
        # 3 bullish + 2 bearish = consensus = 1/5 = 0.2 < 0.3
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bullish_df(),
            "DOGE/USDT:USDT": _bearish_df(),
            "XRP/USDT:USDT": _bearish_df(),
        }
        adj = consensus.compute(pair_data)
        for sym, val in adj.items():
            assert val == 0.0, f"{sym} should be 0 when |consensus| < 0.3, got {val}"


# ═══════════════════════════════════════════════════════════════════
# 4. Minimum Pairs Threshold
# ═══════════════════════════════════════════════════════════════════


class TestMinPairs:
    """Tests for insufficient pair count."""

    def test_too_few_pairs_no_adjustment(self, consensus: CrossAssetConsensus) -> None:
        """Less than MIN_PAIRS (3) → all zeros."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
        }
        adj = consensus.compute(pair_data)
        assert all(v == 0.0 for v in adj.values())

    def test_exactly_min_pairs_works(self, consensus: CrossAssetConsensus) -> None:
        """Exactly MIN_PAIRS (3) → consensus computed."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bullish_df(),
        }
        adj = consensus.compute(pair_data)
        # 3/3 bullish = consensus 1.0 → adjustments applied
        assert all(v > 0 for v in adj.values())


# ═══════════════════════════════════════════════════════════════════
# 5. Edge Cases
# ═══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge case handling."""

    def test_empty_pair_data(self, consensus: CrossAssetConsensus) -> None:
        """Empty dict → empty result."""
        adj = consensus.compute({})
        assert adj == {}

    def test_all_insufficient_data(self, consensus: CrossAssetConsensus) -> None:
        """All pairs have insufficient data → all zeros."""
        pair_data = {
            "BTC/USDT:USDT": _make_price_df(n=5),
            "ETH/USDT:USDT": _make_price_df(n=5),
            "SOL/USDT:USDT": _make_price_df(n=5),
        }
        adj = consensus.compute(pair_data)
        assert all(v == 0.0 for v in adj.values())

    def test_mixed_valid_invalid_data(self, consensus: CrossAssetConsensus) -> None:
        """Some pairs have valid data, some don't. Valid ones still get adjustments."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bullish_df(),
            "DOGE/USDT:USDT": _make_price_df(n=5),  # Insufficient
        }
        adj = consensus.compute(pair_data)
        # 3 valid bullish pairs → consensus = 1.0
        assert adj["BTC/USDT:USDT"] > 0
        assert adj["DOGE/USDT:USDT"] == 0.0  # No direction computed

    def test_adjustment_bounded(self, consensus: CrossAssetConsensus) -> None:
        """Adjustment magnitude is at most MAX_ADJUSTMENT."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),
            "ETH/USDT:USDT": _bullish_df(),
            "SOL/USDT:USDT": _bullish_df(),
        }
        adj = consensus.compute(pair_data)
        for val in adj.values():
            assert abs(val) <= consensus.MAX_ADJUSTMENT + 0.01


# ═══════════════════════════════════════════════════════════════════
# 6. Momentum Confirmation (close vs fast EMA)
# ═══════════════════════════════════════════════════════════════════


def _weakening_uptrend_df(n: int = 100) -> pd.DataFrame:
    """Prices rise steadily then final close dips below fast EMA.

    EMA_fast(8) > EMA_slow(21) (trend is up), but the last close
    drops below EMA_fast → direction = 0 (neutral/weakening).
    Gentle dip ensures the EMA crossover does NOT happen.
    """
    # Rise from 900 to 1100 over 99 candles
    rising = np.linspace(900.0, 1100.0, n - 1)
    # Single candle dip: below EMA_fast (~1092) but above EMA_slow (~1070)
    closes = np.append(rising, [1060.0])
    return pd.DataFrame({"close": closes})


def _weakening_downtrend_df(n: int = 100) -> pd.DataFrame:
    """Prices fall steadily then final close bounces above fast EMA.

    EMA_fast(8) < EMA_slow(21) (trend is down), but the last close
    rises above EMA_fast → direction = 0 (neutral/weakening).
    Gentle bounce ensures the EMA crossover does NOT happen.
    """
    # Fall from 1100 to 900 over 99 candles
    falling = np.linspace(1100.0, 900.0, n - 1)
    # Single candle bounce: above EMA_fast (~908) but below EMA_slow (~930)
    closes = np.append(falling, [940.0])
    return pd.DataFrame({"close": closes})


class TestMomentumConfirmation:
    """Tests for the close-vs-EMA momentum confirmation added to _get_direction."""

    def test_weakening_uptrend_returns_neutral(
        self, consensus: CrossAssetConsensus
    ) -> None:
        """Uptrend with close below fast EMA → direction = 0."""
        df = _weakening_uptrend_df()
        assert consensus._get_direction(df) == 0

    def test_weakening_downtrend_returns_neutral(
        self, consensus: CrossAssetConsensus
    ) -> None:
        """Downtrend with close above fast EMA → direction = 0."""
        df = _weakening_downtrend_df()
        assert consensus._get_direction(df) == 0

    def test_strong_uptrend_still_bullish(
        self, consensus: CrossAssetConsensus
    ) -> None:
        """Linearly rising prices → close above fast EMA → direction = +1."""
        df = _bullish_df()
        assert consensus._get_direction(df) == 1

    def test_strong_downtrend_still_bearish(
        self, consensus: CrossAssetConsensus
    ) -> None:
        """Linearly falling prices → close below fast EMA → direction = -1."""
        df = _bearish_df()
        assert consensus._get_direction(df) == -1

    def test_consensus_diluted_by_neutral_pairs(
        self, consensus: CrossAssetConsensus
    ) -> None:
        """Neutral (weakening) pairs dilute consensus score toward 0."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),        # +1
            "ETH/USDT:USDT": _weakening_uptrend_df(),  # 0
            "SOL/USDT:USDT": _weakening_uptrend_df(),  # 0
            "DOGE/USDT:USDT": _weakening_uptrend_df(), # 0
        }
        adj = consensus.compute(pair_data)
        # Consensus = (1+0+0+0)/4 = 0.25 < threshold 0.3 → no adjustment
        for val in adj.values():
            assert val == 0.0

    def test_majority_neutral_weakens_consensus(
        self, consensus: CrossAssetConsensus
    ) -> None:
        """Even if some pairs are bullish, many neutral pairs weaken consensus."""
        pair_data = {
            "BTC/USDT:USDT": _bullish_df(),        # +1
            "ETH/USDT:USDT": _bullish_df(),        # +1
            "SOL/USDT:USDT": _weakening_uptrend_df(),  # 0
            "DOGE/USDT:USDT": _weakening_uptrend_df(), # 0
            "XRP/USDT:USDT": _weakening_uptrend_df(),  # 0
        }
        adj = consensus.compute(pair_data)
        # Consensus = (1+1+0+0+0)/5 = 0.4 > threshold 0.3
        # BTC/ETH aligned: positive adj. Neutral pairs: 0 adj.
        assert adj["BTC/USDT:USDT"] > 0
        assert adj["ETH/USDT:USDT"] > 0
        assert adj["SOL/USDT:USDT"] == 0.0  # Neutral: 0 * sign(0.4) = 0
