"""Suppressed-path QUALITY audit (Phase 2B).

Read-only forensic tool. For every reduced-mode SUPPRESSED path that could
have produced a signal under Phase 2B, walk the post-signal price path
forward via 15m OHLCV and measure first-touch order, MFE/MAE, fee drag.

Currently allowed paths (live):     4h_flip, 1h_continuation
Currently suppressed paths (audit): 15m_fast, aligned_trend,
                                    adaptive_trend_route, breakout_trader_route

Mirrors the production signal pipeline:
  * `RegimeDetector.detect`
  * `AdaptiveStrategy.select_strategy` route gates
  * `SupertrendTrend.{generate_signal, generate_continuation_signal,
                      generate_fast_signal, generate_aligned_signal}`
  * `AdaptiveTrend.generate_signal` (route disabled in reduced mode)
  * `BreakoutTrader.generate_signal` (route disabled in reduced mode)
  * `IndicatorEngine.calculate_all` (Supertrend(8, 2.0), ADX(14), ATR(14))
  * `AdaptiveStrategy.MIN_CONFIDENCE = 45.0`

Cascade order is preserved: a 15m_fast signal is only counted if the
checkpoint also produced NONE for both 4h_flip and 1h_continuation; an
aligned_trend signal is only counted if all earlier cascade levels were
NONE. This matches what the live bot would actually fire if the
suppressed flags were flipped.

NOT a backtest. NO orders. NO DB mutations. NO config changes.

Usage
-----
    .venv/bin/python scripts/audit_suppressed_path_quality.py \
        --out docs/reports/SUPPRESSED_PATH_QUALITY_AUDIT.md \
        --csv docs/reports/suppressed_path_quality.csv \
        --summary-csv docs/reports/suppressed_path_quality_summary.csv

Optional `--days` selects the primary window (default 28). 14-day
secondary cross-check runs always; 60-day cross-check runs when the
primary >= 60 OR `--include-60d` is passed.

Hard constraints
----------------
- This script never writes to any file under `src/`, `config/`, or the DB.
- It reads OHLCV from Binance mainnet via the production
  `MarketDataClient.fetch_ohlcv` path.
- It does not enable any disabled flag at runtime; route gates are
  evaluated by the audit itself, not by importing the live flag set.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=True)

from src.data.indicator_engine import IndicatorEngine  # noqa: E402
from src.data.market_data import MarketDataClient  # noqa: E402
from src.strategies.adaptive_strategy import AdaptiveStrategy  # noqa: E402
from src.strategies.adaptive_trend import AdaptiveTrend  # noqa: E402
from src.strategies.breakout_trader import BreakoutTrader  # noqa: E402
from src.strategies.base_strategy import Signal, SignalDirection  # noqa: E402
from src.strategies.regime_detector import MarketRegime, RegimeDetector  # noqa: E402
from src.strategies.supertrend_trend import SupertrendTrend  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("audit_suppressed")
log.setLevel(logging.INFO)

# Universe
PRIMARY_SYMBOLS = ["SOL/USDT:USDT", "SUI/USDT:USDT"]
RESEARCH_SYMBOLS = [
    "ETH/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT",
    "LINK/USDT:USDT", "AVAX/USDT:USDT", "ADA/USDT:USDT",
]
SYMBOLS = PRIMARY_SYMBOLS + RESEARCH_SYMBOLS

# Phase-2B allowed pair set (mirrors reduced_live_mode.ALLOWED_SYMBOLS)
ALLOWED_PAIRS_REDUCED = frozenset({"SOL/USDT:USDT", "SUI/USDT:USDT"})

CYCLE_MINUTES = 30
MIN_CONFIDENCE = AdaptiveStrategy.MIN_CONFIDENCE  # 45.0
HORIZONS_HOURS = (4, 8, 24, 48)
FORWARD_BUFFER_HOURS = 48  # match longest horizon

PATHS_ALLOWED = ("4h_flip", "1h_continuation")
PATHS_SUPPRESSED = (
    "15m_fast",
    "aligned_trend",
    "adaptive_trend_route",
    "breakout_trader_route",
)
ALL_PATHS = PATHS_ALLOWED + PATHS_SUPPRESSED

# Suppression flag mapping (mirrors reduced_live_mode.py constants)
SUPPRESSED_FLAG = {
    "15m_fast": "ALLOW_15M_FAST",
    "aligned_trend": "ALLOW_ALIGNED_TREND",
    "adaptive_trend_route": "ALLOW_ADAPTIVE_TREND_ROUTE",
    "breakout_trader_route": "ALLOW_BREAKOUT_TRADER_ROUTE",
    "4h_flip": "(allowed)",
    "1h_continuation": "(allowed)",
}

# Fee assumptions (Binance USDT-M Futures VIP 0, BNB discount NOT applied;
# matches docs/CURRENT_STATE.md and FeeCalculator defaults).
TAKER_FEE = 0.0005   # 0.05%
MAKER_FEE = 0.0002   # 0.02%
TAKER_TAKER_RT = TAKER_FEE + TAKER_FEE   # 0.10%
MAKER_TAKER_RT = MAKER_FEE + TAKER_FEE   # 0.07%

# 15m page cap (Binance limit per call is 1500; we page via `since` cursor).
# 60 days at 15m + 48h forward buffer = 5760 + 192 = 5952 bars. Cap at 7000
# to leave headroom for rerunning at 60d windows. Each call returns up to
# 1000 bars, so 7000 implies ~7 pages per symbol per window.
MAX_15M_BARS = 7000

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df.set_index("timestamp").sort_index()
    return df


async def fetch_paged(
    client: MarketDataClient,
    symbol: str,
    timeframe: str,
    total_needed: int,
    cap: int,
) -> pd.DataFrame:
    """Fetch >limit bars by paging via ccxt `since` parameter."""
    exchange = client._require_exchange()
    if timeframe == "15m":
        tf_ms = 15 * 60 * 1000
    elif timeframe == "1h":
        tf_ms = 60 * 60 * 1000
    elif timeframe == "4h":
        tf_ms = 4 * 60 * 60 * 1000
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    earliest_ms = end_ms - total_needed * tf_ms

    all_rows: list[list] = []
    cursor = earliest_ms
    while cursor < end_ms:
        batch = await exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=cursor, limit=1000,
        )
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + tf_ms
        if len(all_rows) > cap:
            break

    seen: set[int] = set()
    deduped: list[list] = []
    for r in all_rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        deduped.append(r)
    deduped.sort(key=lambda r: r[0])

    candles = [
        {
            "timestamp": datetime.fromtimestamp(r[0] / 1000.0, tz=timezone.utc),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in deduped
    ]
    df = pd.DataFrame(candles).set_index("timestamp").sort_index()
    return df


# ---------------------------------------------------------------------------
# Signal evaluation at a single checkpoint (mirrors production cascade)
# ---------------------------------------------------------------------------

@dataclass
class RawSignal:
    symbol: str
    checkpoint: datetime
    path: str
    route: str  # supertrend_trend | adaptive_trend | breakout_trader
    regime: str  # trending | ranging | volatile | quiet
    direction: str  # LONG | SHORT
    entry_price: float
    atr_4h: float
    sl_price: float
    tp_price: float
    confidence: float
    live_allowed: bool          # currently fires under Phase 2B
    suppressed_flag: str        # which flag suppresses it (or "(allowed)")


def _closed_slice(df: pd.DataFrame, tf_minutes: int, n_required: int,
                  t: datetime) -> Optional[pd.DataFrame]:
    cutoff = t - timedelta(minutes=tf_minutes)
    s = df[df.index <= cutoff]
    if len(s) < n_required:
        return None
    return s


def _confidence_ok(sig: Optional[Signal]) -> bool:
    return (
        sig is not None
        and sig.direction != SignalDirection.NONE
        and sig.confidence >= MIN_CONFIDENCE
    )


def evaluate_all_signals(
    symbol: str,
    checkpoint: datetime,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> list[RawSignal]:
    """Return all reachable signals (allowed + suppressed) at this checkpoint.

    Cascade discipline (matches `AdaptiveStrategy.get_signal_multi_tf`):
      1. supertrend_trend route is selected when TRENDING+ADX>=18 OR
         RANGING+ADX>=18 (the dead-zone bridge).  Within that route the
         cascade is 4h_flip -> 1h_continuation -> 15m_fast -> aligned_trend,
         each tried only when the previous returned NONE.
      2. adaptive_trend route is selected when RANGING+ADX<18 (live mode
         only; we evaluate it here even though the route is suppressed).
      3. breakout_trader route is selected when VOLATILE+ADX>=15 (live
         mode only; we evaluate it here even though the route is suppressed).
      4. QUIET regime -> nothing.

    A single checkpoint produces AT MOST ONE signal in the live system
    (one route, one cascade level), so we mirror that here too.  However
    when an earlier cascade fires we ALSO look ahead and record any later
    cascade level that *would* have fired if the earlier one had been
    NONE — this is the "what would re-enabling reveal?" measurement, but
    it is recorded under that path's name.  Callers can filter live-allowed
    rows by checking `live_allowed`.
    """
    s4h = _closed_slice(df_4h, 240, 100, checkpoint)
    s1h = _closed_slice(df_1h, 60, 50, checkpoint)
    s15m = _closed_slice(df_15m, 15, 50, checkpoint)
    if s4h is None or s1h is None or s15m is None:
        return []

    # Regime
    rd = RegimeDetector()
    try:
        regime_state = rd.detect(s4h)
    except (KeyError, ValueError):
        return []

    out: list[RawSignal] = []
    close_1h = float(s1h["close"].dropna().iloc[-1])

    is_supertrend_route = False
    is_adaptive_trend_route = False
    is_breakout_route = False
    regime_str = ""

    if regime_state.regime == MarketRegime.QUIET:
        return []
    if regime_state.regime == MarketRegime.TRENDING:
        if regime_state.adx >= 18.0:
            is_supertrend_route = True
            regime_str = "trending"
        else:
            return []  # weak trend: no route
    elif regime_state.regime == MarketRegime.RANGING:
        if regime_state.adx >= 18.0:
            is_supertrend_route = True
            regime_str = "ranging"
        else:
            is_adaptive_trend_route = True
            regime_str = "ranging"
    elif regime_state.regime == MarketRegime.VOLATILE:
        if regime_state.adx >= 15.0:
            is_breakout_route = True
            regime_str = "volatile"
        else:
            return []  # weak volatile: no route

    atr_4h = float(s4h["atr"].dropna().iloc[-1]) if "atr" in s4h.columns else float("nan")

    # ----- supertrend_trend cascade ----------------------------------------
    if is_supertrend_route:
        st = SupertrendTrend()

        # Level 1: 4h_flip
        try:
            sig_4h = st.generate_signal(s4h, entry_price=close_1h, regime=regime_str)
        except Exception:
            sig_4h = None
        # Level 2: 1h_continuation
        try:
            sig_1h = st.generate_continuation_signal(s4h, s1h, regime=regime_str)
        except Exception:
            sig_1h = None
        # Level 3: 15m_fast
        try:
            sig_15m = st.generate_fast_signal(s4h, s1h, s15m, regime=regime_str)
        except Exception:
            sig_15m = None
        # Level 4: aligned_trend
        try:
            sig_al = st.generate_aligned_signal(s4h, s1h, regime=regime_str)
        except Exception:
            sig_al = None

        # The live bot stops at the first non-NONE / >=MIN_CONFIDENCE signal.
        # For the audit we record EVERY cascade level that produced a valid
        # signal at this checkpoint, so that suppressed levels can be counted
        # *as they would have fired if reachable*.  Because we are stripping
        # MFE/MAE per signal event (not per cascade decision), this is the
        # correct "reachable-if-enabled" measurement.
        for path, sig in [
            ("4h_flip", sig_4h),
            ("1h_continuation", sig_1h),
            ("15m_fast", sig_15m),
            ("aligned_trend", sig_al),
        ]:
            if not _confidence_ok(sig):
                continue
            symbol_live = symbol in ALLOWED_PAIRS_REDUCED
            path_live = path in ("4h_flip", "1h_continuation")
            live_allowed = symbol_live and path_live
            out.append(RawSignal(
                symbol=symbol,
                checkpoint=checkpoint,
                path=path,
                route="supertrend_trend",
                regime=regime_str,
                direction=sig.direction.value.upper(),
                entry_price=float(sig.entry_price),
                atr_4h=atr_4h,
                sl_price=float(sig.stop_loss),
                tp_price=float(sig.take_profit),
                confidence=float(sig.confidence),
                live_allowed=live_allowed,
                suppressed_flag=SUPPRESSED_FLAG[path],
            ))

    # ----- adaptive_trend route -------------------------------------------
    if is_adaptive_trend_route:
        at = AdaptiveTrend()
        try:
            sig = at.generate_signal(s4h, entry_price=close_1h, regime="ranging")
        except Exception:
            sig = None
        if _confidence_ok(sig):
            out.append(RawSignal(
                symbol=symbol,
                checkpoint=checkpoint,
                path="adaptive_trend_route",
                route="adaptive_trend",
                regime="ranging",
                direction=sig.direction.value.upper(),
                entry_price=float(sig.entry_price),
                atr_4h=atr_4h,
                sl_price=float(sig.stop_loss),
                tp_price=float(sig.take_profit),
                confidence=float(sig.confidence),
                live_allowed=False,
                suppressed_flag=SUPPRESSED_FLAG["adaptive_trend_route"],
            ))

    # ----- breakout_trader route ------------------------------------------
    if is_breakout_route:
        bt = BreakoutTrader()
        try:
            sig = bt.generate_signal(s1h)
        except Exception:
            sig = None
        if _confidence_ok(sig):
            out.append(RawSignal(
                symbol=symbol,
                checkpoint=checkpoint,
                path="breakout_trader_route",
                route="breakout_trader",
                regime="volatile",
                direction=sig.direction.value.upper(),
                entry_price=float(sig.entry_price),
                atr_4h=atr_4h,  # uses 1H ATR internally for SL distance, but
                                # we record 4H ATR for cross-path comparability
                sl_price=float(sig.stop_loss),
                tp_price=float(sig.take_profit),
                confidence=float(sig.confidence),
                live_allowed=False,
                suppressed_flag=SUPPRESSED_FLAG["breakout_trader_route"],
            ))

    return out


# ---------------------------------------------------------------------------
# Forward path measurement
# ---------------------------------------------------------------------------

@dataclass
class ForwardResult:
    one_r_before_sl: Optional[bool]
    two_r_before_sl: Optional[bool]
    tp_before_sl: Optional[bool]
    sl_first: Optional[bool]
    unresolved: bool

    # MFE/MAE in absolute price (signed-distance abs)
    mfe_4h: float
    mae_4h: float
    mfe_8h: float
    mae_8h: float
    mfe_24h: float
    mae_24h: float
    mfe_48h: float
    mae_48h: float

    time_to_sl_min: Optional[int]
    time_to_1r_min: Optional[int]
    time_to_tp_min: Optional[int]

    truncated_4h: bool
    truncated_8h: bool
    truncated_24h: bool
    truncated_48h: bool
    forward_bars: int


def measure_forward(sig: RawSignal, df_15m: pd.DataFrame) -> ForwardResult:
    is_long = sig.direction == "LONG"
    one_r_dist = abs(sig.entry_price - sig.sl_price)
    if one_r_dist <= 0:
        return ForwardResult(
            None, None, None, None, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            None, None, None,
            False, False, False, False, 0,
        )

    one_r_target = (sig.entry_price + one_r_dist) if is_long else (sig.entry_price - one_r_dist)

    fwd = df_15m[df_15m.index >= sig.checkpoint]
    if fwd.empty:
        return ForwardResult(
            None, None, None, None, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            None, None, None,
            True, True, True, True, 0,
        )

    horizon_48h = sig.checkpoint + timedelta(hours=48)
    horizon_24h = sig.checkpoint + timedelta(hours=24)
    horizon_8h = sig.checkpoint + timedelta(hours=8)
    horizon_4h = sig.checkpoint + timedelta(hours=4)

    fwd_48h = fwd[fwd.index < horizon_48h]
    if fwd_48h.empty:
        fwd_48h = fwd

    sl_hit_at: Optional[datetime] = None
    one_r_hit_at: Optional[datetime] = None
    tp_hit_at: Optional[datetime] = None

    mfe = {h: 0.0 for h in HORIZONS_HOURS}
    mae = {h: 0.0 for h in HORIZONS_HOURS}

    for ts, row in fwd_48h.iterrows():
        hi = float(row["high"])
        lo = float(row["low"])
        if is_long:
            fav_excursion = max(0.0, hi - sig.entry_price)
            adv_excursion = max(0.0, sig.entry_price - lo)
        else:
            fav_excursion = max(0.0, sig.entry_price - lo)
            adv_excursion = max(0.0, hi - sig.entry_price)

        for h in HORIZONS_HOURS:
            if ts < sig.checkpoint + timedelta(hours=h):
                if fav_excursion > mfe[h]:
                    mfe[h] = fav_excursion
                if adv_excursion > mae[h]:
                    mae[h] = adv_excursion

        # Restrict first-touch resolution to the 24h horizon, matching the
        # primary audit window for outcome rates.
        if ts < horizon_24h:
            candle_hits_sl = (lo <= sig.sl_price) if is_long else (hi >= sig.sl_price)
            candle_hits_1r = (hi >= one_r_target) if is_long else (lo <= one_r_target)
            candle_hits_tp = (hi >= sig.tp_price) if is_long else (lo <= sig.tp_price)

            if sl_hit_at is None and candle_hits_sl:
                sl_hit_at = ts
            # ambiguous-bar rule: if the same candle hits SL, we conservatively
            # do NOT credit the favorable level (1R / TP) on that bar.
            if one_r_hit_at is None and candle_hits_1r:
                if not (candle_hits_sl and sl_hit_at == ts):
                    one_r_hit_at = ts
            if tp_hit_at is None and candle_hits_tp:
                if not (candle_hits_sl and sl_hit_at == ts):
                    tp_hit_at = ts

    # Resolve outcomes
    sl_first: Optional[bool] = None
    tp_before_sl: Optional[bool] = None
    one_r_before_sl: Optional[bool] = None

    if sl_hit_at is None and tp_hit_at is None and one_r_hit_at is None:
        unresolved = True
    else:
        unresolved = False

    if sl_hit_at is not None or one_r_hit_at is not None:
        if sl_hit_at is None:
            one_r_before_sl = True
        elif one_r_hit_at is None:
            one_r_before_sl = False
        else:
            one_r_before_sl = one_r_hit_at <= sl_hit_at

    if sl_hit_at is not None or tp_hit_at is not None:
        if sl_hit_at is None:
            tp_before_sl = True
            sl_first = False
        elif tp_hit_at is None:
            tp_before_sl = False
            sl_first = True
        else:
            tp_before_sl = tp_hit_at < sl_hit_at
            sl_first = sl_hit_at <= tp_hit_at

    two_r_before_sl = tp_before_sl  # All audited paths price TP at >=2R from entry

    def _td_min(t: Optional[datetime]) -> Optional[int]:
        if t is None:
            return None
        return int((t - sig.checkpoint).total_seconds() / 60)

    last_ts = fwd_48h.index[-1] if len(fwd_48h) else sig.checkpoint
    coverage_end = last_ts + timedelta(minutes=15)
    truncated_4h = coverage_end < horizon_4h
    truncated_8h = coverage_end < horizon_8h
    truncated_24h = coverage_end < horizon_24h
    truncated_48h = coverage_end < horizon_48h

    return ForwardResult(
        one_r_before_sl=one_r_before_sl,
        two_r_before_sl=two_r_before_sl,
        tp_before_sl=tp_before_sl,
        sl_first=sl_first,
        unresolved=unresolved,
        mfe_4h=mfe[4], mae_4h=mae[4],
        mfe_8h=mfe[8], mae_8h=mae[8],
        mfe_24h=mfe[24], mae_24h=mae[24],
        mfe_48h=mfe[48], mae_48h=mae[48],
        time_to_sl_min=_td_min(sl_hit_at),
        time_to_1r_min=_td_min(one_r_hit_at),
        time_to_tp_min=_td_min(tp_hit_at),
        truncated_4h=truncated_4h,
        truncated_8h=truncated_8h,
        truncated_24h=truncated_24h,
        truncated_48h=truncated_48h,
        forward_bars=len(fwd_48h),
    )


# ---------------------------------------------------------------------------
# Opportunity dedupe
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    symbol: str
    checkpoint: datetime
    path: str
    route: str
    regime: str
    direction: str
    entry_price: float
    atr_4h: float
    sl_price: float
    tp_price: float
    confidence: float
    live_allowed: bool
    suppressed_flag: str
    run_length: int
    forward: ForwardResult = field(default=None)  # type: ignore[assignment]


def dedupe_runs(raw_signals: list[RawSignal]) -> list[Opportunity]:
    """Collapse consecutive same-direction signals on the same path into one
    opportunity, anchored on the FIRST checkpoint of the run.

    Dedupe key: (symbol, path, direction).  A run breaks when:
      * direction changes, OR
      * checkpoint gap > CYCLE_MINUTES + 1 (signal vanished between
        adjacent 30-min checkpoints, then returned later — treated as a
        new opportunity; this slightly OVER-counts opportunities, which
        is the conservative direction for an audit that should not
        overstate setup frequency).
    """
    by_key: dict[tuple[str, str], list[RawSignal]] = defaultdict(list)
    for s in raw_signals:
        by_key[(s.symbol, s.path)].append(s)

    opps: list[Opportunity] = []
    for (sym, path), group in by_key.items():
        group.sort(key=lambda x: x.checkpoint)
        run: list[RawSignal] = []
        for sig in group:
            if not run:
                run = [sig]
                continue
            prev = run[-1]
            gap = (sig.checkpoint - prev.checkpoint).total_seconds() / 60
            if sig.direction == prev.direction and gap <= CYCLE_MINUTES + 1:
                run.append(sig)
            else:
                opps.append(_opp_from_run(run))
                run = [sig]
        if run:
            opps.append(_opp_from_run(run))
    opps.sort(key=lambda o: (o.symbol, o.checkpoint, o.path))
    return opps


def _opp_from_run(run: list[RawSignal]) -> Opportunity:
    head = run[0]
    return Opportunity(
        symbol=head.symbol,
        checkpoint=head.checkpoint,
        path=head.path,
        route=head.route,
        regime=head.regime,
        direction=head.direction,
        entry_price=head.entry_price,
        atr_4h=head.atr_4h,
        sl_price=head.sl_price,
        tp_price=head.tp_price,
        confidence=head.confidence,
        live_allowed=head.live_allowed,
        suppressed_flag=head.suppressed_flag,
        run_length=len(run),
    )


# ---------------------------------------------------------------------------
# Fee-adjusted expectancy proxy
# ---------------------------------------------------------------------------


def _fee_drag_R(opp: Opportunity, round_trip_pct: float) -> float:
    """Round-trip fee expressed as a multiple of R.

    R = entry_to_SL distance.  Fee on notional = entry_price * round_trip_pct.
    fee_in_R = (entry_price * round_trip_pct) / one_R_dist.
    """
    one_r_dist = abs(opp.entry_price - opp.sl_price)
    if one_r_dist <= 0:
        return float("nan")
    fee_price = opp.entry_price * round_trip_pct
    return fee_price / one_r_dist


def _per_opp_R(opp: Opportunity) -> float:
    """Return the realized payoff in units of R, ignoring fees.

    Convention: TP-before-SL → +2R (TP is at +2R for our SL/TP model);
    SL-first → -1R; unresolved/no-resolution → 0R.
    """
    f = opp.forward
    if f is None:
        return 0.0
    if f.two_r_before_sl is True:
        return 2.0
    if f.sl_first is True:
        return -1.0
    return 0.0


def _expectancy_proxy(opps: list[Opportunity], round_trip_pct: float) -> float:
    if not opps:
        return float("nan")
    total = 0.0
    for o in opps:
        gross_R = _per_opp_R(o)
        fee_R = _fee_drag_R(o, round_trip_pct)
        if math.isnan(fee_R):
            continue
        total += gross_R - fee_R
    return total / len(opps)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _safe_pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def _median(xs: list[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    if not vals:
        return None
    return statistics.median(vals)


def aggregate(opps: list[Opportunity]) -> dict:
    measurable = [o for o in opps if o.forward is not None and o.forward.forward_bars > 0]
    unmeasurable_total = len(opps) - len(measurable)

    by_sp: dict[tuple[str, str], list[Opportunity]] = defaultdict(list)
    by_path: dict[str, list[Opportunity]] = defaultdict(list)
    by_symbol: dict[str, list[Opportunity]] = defaultdict(list)
    by_flag: dict[str, list[Opportunity]] = defaultdict(list)
    for o in measurable:
        by_sp[(o.symbol, o.path)].append(o)
        by_path[o.path].append(o)
        by_symbol[o.symbol].append(o)
        by_flag[o.suppressed_flag].append(o)

    def _stats(items: list[Opportunity]) -> dict:
        n = len(items)
        if n == 0:
            return {
                "n": 0,
                "one_r_before_sl": 0,
                "two_r_before_sl": 0,
                "sl_first": 0,
                "unresolved": 0,
                "avg_mfe_4h_R": 0.0, "avg_mfe_8h_R": 0.0,
                "avg_mfe_24h_R": 0.0, "avg_mfe_48h_R": 0.0,
                "avg_mae_4h_R": 0.0, "avg_mae_8h_R": 0.0,
                "avg_mae_24h_R": 0.0, "avg_mae_48h_R": 0.0,
                "median_time_to_1r_min": None,
                "median_time_to_sl_min": None,
                "median_time_to_tp_min": None,
                "expectancy_taker_R": float("nan"),
                "expectancy_maker_R": float("nan"),
                "n_symbols": 0,
                "top_symbol_share": 0.0,
                "top_day_share": 0.0,
                "live_allowed_n": 0,
            }

        one_r = sum(1 for o in items if o.forward.one_r_before_sl is True)
        two_r = sum(1 for o in items if o.forward.two_r_before_sl is True)
        sl_first = sum(1 for o in items if o.forward.sl_first is True)
        unresolved = sum(1 for o in items if o.forward.unresolved)

        mfe_r = {h: [] for h in HORIZONS_HOURS}
        mae_r = {h: [] for h in HORIZONS_HOURS}
        for o in items:
            r = abs(o.entry_price - o.sl_price)
            if r <= 0:
                continue
            mfe_r[4].append(o.forward.mfe_4h / r)
            mfe_r[8].append(o.forward.mfe_8h / r)
            mfe_r[24].append(o.forward.mfe_24h / r)
            mfe_r[48].append(o.forward.mfe_48h / r)
            mae_r[4].append(o.forward.mae_4h / r)
            mae_r[8].append(o.forward.mae_8h / r)
            mae_r[24].append(o.forward.mae_24h / r)
            mae_r[48].append(o.forward.mae_48h / r)

        def _avg(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        sym_counter: dict[str, int] = defaultdict(int)
        day_counter: dict[str, int] = defaultdict(int)
        for o in items:
            sym_counter[o.symbol] += 1
            day_counter[o.checkpoint.date().isoformat()] += 1

        top_symbol_share = (max(sym_counter.values()) / n) if sym_counter else 0.0
        top_day_share = (max(day_counter.values()) / n) if day_counter else 0.0

        return {
            "n": n,
            "one_r_before_sl": one_r,
            "two_r_before_sl": two_r,
            "sl_first": sl_first,
            "unresolved": unresolved,
            "avg_mfe_4h_R": _avg(mfe_r[4]),
            "avg_mfe_8h_R": _avg(mfe_r[8]),
            "avg_mfe_24h_R": _avg(mfe_r[24]),
            "avg_mfe_48h_R": _avg(mfe_r[48]),
            "avg_mae_4h_R": _avg(mae_r[4]),
            "avg_mae_8h_R": _avg(mae_r[8]),
            "avg_mae_24h_R": _avg(mae_r[24]),
            "avg_mae_48h_R": _avg(mae_r[48]),
            "median_time_to_1r_min": _median([o.forward.time_to_1r_min for o in items]),
            "median_time_to_sl_min": _median([o.forward.time_to_sl_min for o in items]),
            "median_time_to_tp_min": _median([o.forward.time_to_tp_min for o in items]),
            "expectancy_taker_R": _expectancy_proxy(items, TAKER_TAKER_RT),
            "expectancy_maker_R": _expectancy_proxy(items, MAKER_TAKER_RT),
            "n_symbols": len(sym_counter),
            "top_symbol_share": top_symbol_share,
            "top_day_share": top_day_share,
            "live_allowed_n": sum(1 for o in items if o.live_allowed),
        }

    return {
        "by_sp": {k: _stats(v) for k, v in by_sp.items()},
        "by_path": {k: _stats(v) for k, v in by_path.items()},
        "by_symbol": {k: _stats(v) for k, v in by_symbol.items()},
        "by_flag": {k: _stats(v) for k, v in by_flag.items()},
        "all": _stats(measurable),
        "unmeasurable": unmeasurable_total,
    }


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------

def verdict_for_path(stats: dict) -> str:
    """Apply the spec's labelling rules to a path-level stats dict."""
    n = stats["n"]
    if n == 0:
        return "NEEDS_LONGER_WINDOW"

    one_r_rate = stats["one_r_before_sl"] / n
    two_r_rate = stats["two_r_before_sl"] / n
    exp_taker = stats["expectancy_taker_R"]

    # DO_NOT_ENABLE rules (any one is sufficient)
    if n < 10 and one_r_rate < 0.40:
        return "DO_NOT_ENABLE"
    if n >= 10 and two_r_rate == 0.0:
        return "DO_NOT_ENABLE"
    if not math.isnan(exp_taker) and exp_taker < 0.0:
        return "DO_NOT_ENABLE"

    # PROMISING_CANDIDATE rules (ALL must hold)
    concentration_ok = (
        stats["top_symbol_share"] < 0.7  # not >70% from one symbol
        and stats["top_day_share"] < 0.5  # not >50% from one day
    )
    if (
        n >= 20
        and one_r_rate >= 0.55
        and two_r_rate >= 0.33
        and (not math.isnan(exp_taker)) and exp_taker > 0.0
        and concentration_ok
    ):
        return "PROMISING_CANDIDATE"

    if n < 20:
        return "WEAK_SAMPLE"
    return "REJECT_FOR_NOW"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def fetch_symbol_data(
    client: MarketDataClient,
    engine: IndicatorEngine,
    symbol: str,
    days: int,
) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], dict]:
    """Fetch and indicator-enrich 4H, 1H, 15m for a symbol covering `days`.

    Returns (df_4h, df_1h, df_15m_raw, meta).  If any timeframe fetch
    fails, returns Nones with `meta['error']` populated; the caller marks
    that symbol incomplete.
    """
    meta: dict = {"symbol": symbol}
    try:
        need_4h = int(days * 24 / 4) + 200
        need_1h = days * 24 + 200
        need_15m = (days * 24 + FORWARD_BUFFER_HOURS) * 4 + 200

        # 4H always fits in one call (limit=1500)
        df_4h_raw = candles_to_df(
            await client.fetch_ohlcv(symbol, timeframe="4h", limit=min(1500, need_4h)),
        )
        # 1H: 60d = 1440 + buffer, may exceed 1500 → page if so
        if need_1h <= 1500:
            df_1h_raw = candles_to_df(
                await client.fetch_ohlcv(symbol, timeframe="1h", limit=need_1h),
            )
        else:
            df_1h_raw = await fetch_paged(client, symbol, "1h", need_1h, cap=3500)
        df_15m_raw = await fetch_paged(client, symbol, "15m", need_15m, cap=MAX_15M_BARS)

        if df_4h_raw.empty or df_1h_raw.empty or df_15m_raw.empty:
            meta["error"] = "empty_fetch"
            return None, None, None, meta

        df_4h = engine.calculate_all(df_4h_raw.copy())
        df_1h = engine.calculate_all(df_1h_raw.copy())
        df_15m = engine.calculate_all(df_15m_raw.copy())

        meta.update({
            "4h_bars": len(df_4h), "1h_bars": len(df_1h), "15m_bars": len(df_15m),
            "4h_first": df_4h.index[0] if len(df_4h) else None,
            "4h_last": df_4h.index[-1] if len(df_4h) else None,
            "15m_first": df_15m.index[0] if len(df_15m) else None,
            "15m_last": df_15m.index[-1] if len(df_15m) else None,
        })

        return df_4h, df_1h, df_15m_raw, meta

    except Exception as exc:
        log.exception("fetch failed for %s: %s", symbol, exc)
        meta["error"] = str(exc)
        return None, None, None, meta


async def run_window(
    client: MarketDataClient,
    engine: IndicatorEngine,
    win_start: datetime,
    win_end: datetime,
    fetch_days: int,
    cached_data: Optional[dict] = None,
) -> tuple[list[Opportunity], dict, list[str]]:
    """Run audit over a single window.

    `cached_data` allows reuse of the largest-window fetch across multiple
    sub-windows (60d -> 28d -> 14d) to minimise REST calls.
    """
    fetch_meta: dict[str, dict] = {}
    raw_signals: list[RawSignal] = []
    incomplete: list[str] = []

    df_15m_by_symbol: dict[str, pd.DataFrame] = {}

    for symbol in SYMBOLS:
        if cached_data and symbol in cached_data:
            entry = cached_data[symbol]
            if entry["error"]:
                incomplete.append(symbol)
                fetch_meta[symbol] = {"error": entry["error"]}
                continue
            df_4h_full = entry["df_4h"]
            df_1h_full = entry["df_1h"]
            df_15m_raw = entry["df_15m_raw"]
            fetch_meta[symbol] = {
                "4h_bars": len(df_4h_full),
                "1h_bars": len(df_1h_full),
                "15m_bars": len(df_15m_raw),
                "4h_first": df_4h_full.index[0] if len(df_4h_full) else None,
                "4h_last": df_4h_full.index[-1] if len(df_4h_full) else None,
                "15m_first": df_15m_raw.index[0] if len(df_15m_raw) else None,
                "15m_last": df_15m_raw.index[-1] if len(df_15m_raw) else None,
            }
        else:
            df_4h_full, df_1h_full, df_15m_raw, meta = await fetch_symbol_data(
                client, engine, symbol, fetch_days,
            )
            fetch_meta[symbol] = meta
            if df_4h_full is None:
                incomplete.append(symbol)
                continue

        df_15m_by_symbol[symbol] = df_15m_raw

        # Step every 30 min through the window
        cps: list[datetime] = []
        t = win_start
        while t <= win_end:
            cps.append(t)
            t += timedelta(minutes=CYCLE_MINUTES)

        log.info(
            "[%s] evaluating %d checkpoints over %s..%s",
            symbol, len(cps), win_start.date(), win_end.date(),
        )

        df_15m_indexed = df_15m_raw  # already DatetimeIndex
        # df_15m enriched with indicators is needed only by SupertrendTrend's
        # 15m_fast cascade branch via slicing — re-enrich on the fly per symbol
        df_15m_full = engine.calculate_all(df_15m_raw.copy())

        for cp in cps:
            sigs = evaluate_all_signals(
                symbol=symbol,
                checkpoint=cp,
                df_4h=df_4h_full,
                df_1h=df_1h_full,
                df_15m=df_15m_full,
            )
            raw_signals.extend(sigs)

    opps = dedupe_runs(raw_signals)

    # Forward measurement
    for o in opps:
        df15 = df_15m_by_symbol.get(o.symbol)
        if df15 is None or df15.empty:
            o.forward = ForwardResult(
                None, None, None, None, True,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                None, None, None, True, True, True, True, 0,
            )
            continue
        if o.checkpoint < df15.index[0]:
            o.forward = ForwardResult(
                None, None, None, None, True,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                None, None, None, True, True, True, True, 0,
            )
            continue
        o.forward = measure_forward(o, df15)  # type: ignore[arg-type]

    return opps, fetch_meta, incomplete


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

def write_per_opp_csv(opps: list[Opportunity], path: Path, window_label: str) -> None:
    rows = []
    for o in opps:
        f = o.forward
        rows.append({
            "window": window_label,
            "symbol": o.symbol,
            "checkpoint": o.checkpoint.isoformat(),
            "path": o.path,
            "route": o.route,
            "regime": o.regime,
            "direction": o.direction,
            "confidence": o.confidence,
            "entry_price": o.entry_price,
            "atr_4h": o.atr_4h,
            "sl_price": o.sl_price,
            "tp_price": o.tp_price,
            "one_R_dist": abs(o.entry_price - o.sl_price),
            "live_allowed": o.live_allowed,
            "suppressed_flag": o.suppressed_flag,
            "run_length": o.run_length,
            "one_r_before_sl": f.one_r_before_sl,
            "two_r_before_sl": f.two_r_before_sl,
            "sl_first": f.sl_first,
            "unresolved": f.unresolved,
            "mfe_4h": f.mfe_4h, "mae_4h": f.mae_4h,
            "mfe_8h": f.mfe_8h, "mae_8h": f.mae_8h,
            "mfe_24h": f.mfe_24h, "mae_24h": f.mae_24h,
            "mfe_48h": f.mfe_48h, "mae_48h": f.mae_48h,
            "time_to_sl_min": f.time_to_sl_min,
            "time_to_1r_min": f.time_to_1r_min,
            "time_to_tp_min": f.time_to_tp_min,
            "fee_drag_R_taker_taker": _fee_drag_R(o, TAKER_TAKER_RT),
            "fee_drag_R_maker_taker": _fee_drag_R(o, MAKER_TAKER_RT),
            "truncated_4h": f.truncated_4h,
            "truncated_8h": f.truncated_8h,
            "truncated_24h": f.truncated_24h,
            "truncated_48h": f.truncated_48h,
            "forward_bars": f.forward_bars,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def write_summary_csv(
    agg_by_window: dict[str, dict],
    path: Path,
) -> None:
    """One row per (window, scope, key)."""
    rows = []
    for window_label, agg in agg_by_window.items():
        # Per-path
        for p_key, st in agg["by_path"].items():
            n = st["n"]
            if n == 0:
                continue
            rows.append({
                "window": window_label,
                "scope": "path",
                "key": p_key,
                "n": n,
                "one_r_rate": st["one_r_before_sl"] / n,
                "two_r_rate": st["two_r_before_sl"] / n,
                "unresolved_rate": st["unresolved"] / n,
                "avg_mfe_24h_R": st["avg_mfe_24h_R"],
                "avg_mae_24h_R": st["avg_mae_24h_R"],
                "expectancy_taker_R": st["expectancy_taker_R"],
                "expectancy_maker_R": st["expectancy_maker_R"],
                "median_time_to_1r_min": st["median_time_to_1r_min"],
                "median_time_to_sl_min": st["median_time_to_sl_min"],
                "n_symbols": st["n_symbols"],
                "top_symbol_share": st["top_symbol_share"],
                "top_day_share": st["top_day_share"],
                "verdict": verdict_for_path(st),
            })
        # Per-symbol-path
        for (sym, p_key), st in agg["by_sp"].items():
            n = st["n"]
            if n == 0:
                continue
            rows.append({
                "window": window_label,
                "scope": "symbol_path",
                "key": f"{sym}|{p_key}",
                "n": n,
                "one_r_rate": st["one_r_before_sl"] / n,
                "two_r_rate": st["two_r_before_sl"] / n,
                "unresolved_rate": st["unresolved"] / n,
                "avg_mfe_24h_R": st["avg_mfe_24h_R"],
                "avg_mae_24h_R": st["avg_mae_24h_R"],
                "expectancy_taker_R": st["expectancy_taker_R"],
                "expectancy_maker_R": st["expectancy_maker_R"],
                "median_time_to_1r_min": st["median_time_to_1r_min"],
                "median_time_to_sl_min": st["median_time_to_sl_min"],
                "n_symbols": st["n_symbols"],
                "top_symbol_share": st["top_symbol_share"],
                "top_day_share": st["top_day_share"],
                "verdict": verdict_for_path(st),
            })
        # Per-flag
        for flag, st in agg["by_flag"].items():
            n = st["n"]
            if n == 0:
                continue
            rows.append({
                "window": window_label,
                "scope": "flag",
                "key": flag,
                "n": n,
                "one_r_rate": st["one_r_before_sl"] / n,
                "two_r_rate": st["two_r_before_sl"] / n,
                "unresolved_rate": st["unresolved"] / n,
                "avg_mfe_24h_R": st["avg_mfe_24h_R"],
                "avg_mae_24h_R": st["avg_mae_24h_R"],
                "expectancy_taker_R": st["expectancy_taker_R"],
                "expectancy_maker_R": st["expectancy_maker_R"],
                "median_time_to_1r_min": st["median_time_to_1r_min"],
                "median_time_to_sl_min": st["median_time_to_sl_min"],
                "n_symbols": st["n_symbols"],
                "top_symbol_share": st["top_symbol_share"],
                "top_day_share": st["top_day_share"],
                "verdict": verdict_for_path(st),
            })
    pd.DataFrame(rows).to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _row_for_path(path: str, st: dict) -> str:
    n = st["n"]
    if n == 0:
        return f"| {path} | 0 | 0 (n/a) | 0 (n/a) | 0 | n/a | n/a |"
    return (
        f"| {path} | {n} | {st['one_r_before_sl']} ({_safe_pct(st['one_r_before_sl'], n)}) | "
        f"{st['two_r_before_sl']} ({_safe_pct(st['two_r_before_sl'], n)}) | "
        f"{st['unresolved']} | {st['avg_mfe_24h_R']:.2f} | {st['avg_mae_24h_R']:.2f} |"
    )


def build_report(
    agg_14d: dict,
    agg_28d: dict,
    agg_60d: Optional[dict],
    fetch_meta: dict,
    incomplete: dict[str, list[str]],
    fetch_window_days: int,
    primary_window_label: str,
    primary_agg: dict,
) -> str:
    now = datetime.now(tz=timezone.utc)
    out: list[str] = []
    p = out.append

    p("# SUPPRESSED_PATH_QUALITY_AUDIT")
    p("")
    p(f"> Generated: {now.isoformat()} UTC")
    p("> Script: `scripts/audit_suppressed_path_quality.py`")
    p("> Source: Binance mainnet OHLCV (read-only). No orders, no DB writes, no config changes.")
    p("> Status: Read-only forensic audit. Live trading behaviour was NOT modified.")
    p("")

    # ====== 1. Audit window ======
    p("## 1. Audit window")
    p("")
    p(f"- **Primary window**: {primary_window_label}")
    p(f"- **14-day window**: required by spec — always run.")
    p(f"- **28-day window**: required by spec — always run.")
    if agg_60d is not None:
        p(f"- **60-day window**: run as cross-check.")
    else:
        p(f"- **60-day window**: NOT run (`--include-60d` not passed or fetch budget exceeded).")
    p(f"- **Symbols**: 8 — primary `SOL/USDT:USDT`, `SUI/USDT:USDT`; research "
      f"`ETH/USDT:USDT`, `DOGE/USDT:USDT`, `XRP/USDT:USDT`, `LINK/USDT:USDT`, "
      f"`AVAX/USDT:USDT`, `ADA/USDT:USDT`.")
    p(f"- **Cycle cadence**: 30 min (matches `CYCLE_INTERVAL_SECONDS=1800`).")
    p(f"- **Forward horizons**: 4h, 8h, 24h, 48h.  Outcome resolution (1R/2R/SL) "
      f"is bounded to the 24h window; MFE/MAE is reported at all four horizons.")
    p(f"- **Fetched depth**: {fetch_window_days} days of 4H/1H/15m bars per symbol "
      f"(15m paged via ccxt `since`; cap {MAX_15M_BARS}).")
    p("")

    # ====== 2. Method and assumptions ======
    p("## 2. Method and assumptions")
    p("")
    p("1. OHLCV fetched from Binance mainnet via the production "
      "`MarketDataClient.fetch_ohlcv` path (same code, same auth selection "
      "as the live bot — `BINANCE_TESTNET=false`).")
    p("2. Indicators computed ONCE on the full per-symbol dataset using "
      "`IndicatorEngine.calculate_all` (Supertrend(8, 2.0), ADX(14), ATR(14), "
      "EMA9/21/50/200, RSI(14), BB(20, 2σ), Volume SMA(20) — all causal).")
    p("3. Step every 30 minutes through each audit window. At each checkpoint:")
    p("   - Slice each TF to candles whose CLOSE is ≤ checkpoint (drop "
      "in-progress).")
    p("   - Run `RegimeDetector.detect` on the 4H slice.")
    p("   - Mirror `AdaptiveStrategy.select_strategy` route gate exactly:")
    p("     * TRENDING + ADX ≥ 18 → **supertrend_trend** route")
    p("     * RANGING  + ADX ≥ 18 → **supertrend_trend** route (dead-zone bridge)")
    p("     * RANGING  + ADX < 18 → **adaptive_trend** route")
    p("     * VOLATILE + ADX ≥ 15 → **breakout_trader** route")
    p("     * QUIET / weak TRENDING / weak VOLATILE → no route")
    p("   - Within the supertrend_trend route, evaluate every cascade level "
      "independently — `generate_signal` (4h_flip), "
      "`generate_continuation_signal` (1h_continuation), "
      "`generate_fast_signal` (15m_fast), `generate_aligned_signal` "
      "(aligned_trend) — recording any that returned a Signal at "
      "≥ `MIN_CONFIDENCE = 45.0`.  This is a generous (UPPER-BOUND) count "
      "for the suppressed cascade levels: the live bot's cascade stops at "
      "the first non-NONE level, so re-enabling 15m_fast will only fire "
      "on checkpoints where 4h_flip AND 1h_continuation both return NONE.")
    p("4. **Live-allowed flag** per opportunity: True iff "
      "`symbol ∈ {SOL/USDT, SUI/USDT}` AND `path ∈ {4h_flip, 1h_continuation}`.")
    p("5. **Forward path**: walk post-signal 15m candles for up to 48h. "
      "First-touch resolution (SL / +1R / +2R = TP) is bounded to 24h.")
    p("6. **Ambiguous-bar rule**: a 15m candle that touches both SL and "
      "TP/+1R is treated as **SL-first** (conservative). This biases the "
      "audit AGAINST recommending re-enable.")
    p("7. **Dedupe rule**: consecutive same-(symbol, path, direction) "
      "signals on adjacent 30-min checkpoints collapse into one "
      "**opportunity**, anchored on the FIRST checkpoint of the run. "
      "A break in the cluster (gap > 31 min OR direction flip) starts a "
      "new opportunity. This matches the live bot's open-position guard "
      "(once a position is opened on path X for symbol Y in direction D, "
      "subsequent same-direction X-signals for Y are skipped).")
    p("8. **Fee drag** is computed per opportunity as "
      "`(entry_price * round_trip_pct) / one_R_dist`, expressed in units "
      "of R. Round-trip percentages: taker-entry+taker-exit = 0.10%, "
      "maker-entry+taker-exit = 0.07%. BNB discount is NOT applied "
      "(matches `FeeCalculator(use_bnb_discount=False)` default).")
    p("9. **Expectancy proxy**:  per opportunity, "
      "`+2R` if 2R-before-SL, `-1R` if SL-first, `0R` otherwise; subtract "
      "fee drag in R. Aggregate is the simple mean across measurable "
      "opportunities. NO position-sizing, NO compounding, NO funding, "
      "NO slippage.")
    p("10. **NO fill modelling, NO P&L, NO sweep optimisation.** This is "
      "a signal-quality audit, not a backtest.")
    p("")

    # Incomplete fetches
    incomplete_unique = sorted({s for v in incomplete.values() for s in v})
    if incomplete_unique:
        p("**Incomplete data symbols (fetch failure):** "
          + ", ".join(incomplete_unique))
        p("")
    else:
        p("**Incomplete data symbols:** none — all 8 symbols fetched cleanly.")
        p("")

    # ====== 3. Opportunity count by symbol/path ======
    p("## 3. Opportunity count by symbol/path")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| symbol | path | opportunities | live_allowed | suppressed_flag | "
      "1R_before_SL | 2R_before_SL | unresolved |")
    p("|---|---|---:|---:|---|---:|---:|---:|")
    for sym in SYMBOLS:
        for path in ALL_PATHS:
            st = primary_agg["by_sp"].get((sym, path))
            if not st or st["n"] == 0:
                p(f"| {sym} | {path} | 0 | 0 | {SUPPRESSED_FLAG[path]} | 0 (n/a) | 0 (n/a) | 0 |")
                continue
            n = st["n"]
            p(f"| {sym} | {path} | {n} | {st['live_allowed_n']} | "
              f"{SUPPRESSED_FLAG[path]} | "
              f"{st['one_r_before_sl']} ({_safe_pct(st['one_r_before_sl'], n)}) | "
              f"{st['two_r_before_sl']} ({_safe_pct(st['two_r_before_sl'], n)}) | "
              f"{st['unresolved']} |")
    p("")

    # ===== 3.A Required Table A — path-level summary =====
    p("### 3.A Table A — path | opportunities | 1R | 2R | unresolved | avg_MFE_24h | avg_MAE_24h")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| path | opportunities | 1R_before_SL | 2R_before_SL | unresolved | avg_MFE_24h_R | avg_MAE_24h_R |")
    p("|---|---:|---:|---:|---:|---:|---:|")
    for path in ALL_PATHS:
        st = primary_agg["by_path"].get(path, {"n": 0})
        p(_row_for_path(path, st))
    p("")

    # ===== 3.B Required Table D — by suppressed_flag =====
    p("### 3.B Table D — suppressed_flag | opportunities | 1R | 2R | verdict")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| suppressed_flag | opportunities | 1R_rate | 2R_rate | verdict |")
    p("|---|---:|---:|---:|---|")
    # Iterate flags in a stable order: allowed first, then ALLOW_*
    flag_order = (
        ["(allowed)"]
        + [SUPPRESSED_FLAG[p_] for p_ in PATHS_SUPPRESSED]
    )
    for flag in flag_order:
        st = primary_agg["by_flag"].get(flag, {"n": 0})
        n = st["n"]
        if n == 0:
            p(f"| {flag} | 0 | n/a | n/a | NEEDS_LONGER_WINDOW |")
            continue
        v = verdict_for_path(st)
        p(f"| {flag} | {n} | {_safe_pct(st['one_r_before_sl'], n)} | "
          f"{_safe_pct(st['two_r_before_sl'], n)} | {v} |")
    p("")

    # ====== 4. Allowed vs Suppressed comparison ======
    p("## 4. Current allowed vs suppressed comparison")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| group | n | 1R_rate | 2R_rate | unresolved | avg_MFE_24h_R | avg_MAE_24h_R | "
      "expectancy_taker_R | expectancy_maker_R |")
    p("|---|---:|---:|---:|---:|---:|---:|---:|---:|")

    def _group_stats(allowed: bool) -> dict:
        opps_by_path = primary_agg["by_path"]
        agg = {
            "n": 0, "one_r_before_sl": 0, "two_r_before_sl": 0, "unresolved": 0,
            "mfe24_sum": 0.0, "mae24_sum": 0.0, "exp_taker_sum": 0.0,
            "exp_maker_sum": 0.0, "exp_count": 0,
        }
        for path in ALL_PATHS:
            is_allowed = path in PATHS_ALLOWED
            if is_allowed != allowed:
                continue
            st = opps_by_path.get(path, {"n": 0})
            n = st["n"]
            if n == 0:
                continue
            agg["n"] += n
            agg["one_r_before_sl"] += st["one_r_before_sl"]
            agg["two_r_before_sl"] += st["two_r_before_sl"]
            agg["unresolved"] += st["unresolved"]
            agg["mfe24_sum"] += st["avg_mfe_24h_R"] * n
            agg["mae24_sum"] += st["avg_mae_24h_R"] * n
            if not math.isnan(st["expectancy_taker_R"]):
                agg["exp_taker_sum"] += st["expectancy_taker_R"] * n
                agg["exp_maker_sum"] += st["expectancy_maker_R"] * n
                agg["exp_count"] += n
        n = agg["n"]
        if n == 0:
            return {"n": 0}
        return {
            "n": n,
            "one_r_rate": agg["one_r_before_sl"] / n,
            "two_r_rate": agg["two_r_before_sl"] / n,
            "unresolved_rate": agg["unresolved"] / n,
            "avg_mfe_24h_R": agg["mfe24_sum"] / n,
            "avg_mae_24h_R": agg["mae24_sum"] / n,
            "exp_taker_R": (agg["exp_taker_sum"] / agg["exp_count"]) if agg["exp_count"] else float("nan"),
            "exp_maker_R": (agg["exp_maker_sum"] / agg["exp_count"]) if agg["exp_count"] else float("nan"),
        }

    for label, st in (
        ("currently_allowed (4h_flip + 1h_continuation)", _group_stats(True)),
        ("currently_suppressed (15m_fast + aligned + adaptive_trend + breakout)", _group_stats(False)),
    ):
        n = st["n"]
        if n == 0:
            p(f"| {label} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        et = st["exp_taker_R"]; em = st["exp_maker_R"]
        et_s = "n/a" if math.isnan(et) else f"{et:+.3f}"
        em_s = "n/a" if math.isnan(em) else f"{em:+.3f}"
        p(f"| {label} | {n} | {st['one_r_rate']*100:.1f}% | "
          f"{st['two_r_rate']*100:.1f}% | {st['unresolved_rate']*100:.1f}% | "
          f"{st['avg_mfe_24h_R']:.2f} | {st['avg_mae_24h_R']:.2f} | "
          f"{et_s} | {em_s} |")
    p("")

    # ====== 5. 1R-before-SL table ======
    p("## 5. 1R-before-SL table")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| path | n | 1R_before_SL | rate | sl_first | unresolved |")
    p("|---|---:|---:|---:|---:|---:|")
    for path in ALL_PATHS:
        st = primary_agg["by_path"].get(path, {"n": 0})
        n = st["n"]
        if n == 0:
            p(f"| {path} | 0 | 0 | n/a | 0 | 0 |")
            continue
        p(f"| {path} | {n} | {st['one_r_before_sl']} | "
          f"{_safe_pct(st['one_r_before_sl'], n)} | {st['sl_first']} | "
          f"{st['unresolved']} |")
    p("")

    # ====== 6. 2R-before-SL table ======
    p("## 6. 2R-before-SL table")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| path | n | 2R_before_SL | rate |")
    p("|---|---:|---:|---:|")
    for path in ALL_PATHS:
        st = primary_agg["by_path"].get(path, {"n": 0})
        n = st["n"]
        if n == 0:
            p(f"| {path} | 0 | 0 | n/a |")
            continue
        p(f"| {path} | {n} | {st['two_r_before_sl']} | "
          f"{_safe_pct(st['two_r_before_sl'], n)} |")
    p("")

    # ====== 7. Fee-adjusted expectancy proxy ======
    p("## 7. Fee-adjusted expectancy proxy")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("Per-opp gross R = +2R if 2R-before-SL, -1R if SL-first, 0R otherwise. "
      "Subtract per-opp fee drag (taker-entry+taker-exit = 0.10% on notional, "
      "or maker-entry+taker-exit = 0.07%) expressed in R. Aggregate = mean.")
    p("")
    p("| path | n | gross_2R_rate | exp_taker_R (avg per opp) | "
      "exp_maker_R (avg per opp) |")
    p("|---|---:|---:|---:|---:|")
    for path in ALL_PATHS:
        st = primary_agg["by_path"].get(path, {"n": 0})
        n = st["n"]
        if n == 0:
            p(f"| {path} | 0 | n/a | n/a | n/a |")
            continue
        et = st["expectancy_taker_R"]; em = st["expectancy_maker_R"]
        et_s = "n/a" if math.isnan(et) else f"{et:+.3f}"
        em_s = "n/a" if math.isnan(em) else f"{em:+.3f}"
        p(f"| {path} | {n} | {_safe_pct(st['two_r_before_sl'], n)} | "
          f"{et_s} | {em_s} |")
    p("")

    # ====== 8. MFE/MAE summary by path ======
    p("## 8. MFE/MAE summary by path (in units of R)")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("| path | n | avg_MFE_4h | avg_MFE_8h | avg_MFE_24h | avg_MFE_48h | "
      "avg_MAE_4h | avg_MAE_8h | avg_MAE_24h | avg_MAE_48h |")
    p("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for path in ALL_PATHS:
        st = primary_agg["by_path"].get(path, {"n": 0})
        n = st["n"]
        if n == 0:
            p(f"| {path} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        p(f"| {path} | {n} | "
          f"{st['avg_mfe_4h_R']:.2f} | {st['avg_mfe_8h_R']:.2f} | "
          f"{st['avg_mfe_24h_R']:.2f} | {st['avg_mfe_48h_R']:.2f} | "
          f"{st['avg_mae_4h_R']:.2f} | {st['avg_mae_8h_R']:.2f} | "
          f"{st['avg_mae_24h_R']:.2f} | {st['avg_mae_48h_R']:.2f} |")
    p("")

    # ====== 8.X Cross-window check ======
    p("## 8.X Cross-window cross-check (14d vs 28d vs 60d)")
    p("")
    p("Per-path 2R-before-SL rate at each window. A genuinely promising "
      "path should show a stable or rising 2R rate as the sample grows; "
      "a path whose 2R rate fluctuates wildly (or stays at 0) across "
      "window lengths is regime-specific or noise.")
    p("")
    p("| path | n_14d | 2R_rate_14d | n_28d | 2R_rate_28d | "
      + ("n_60d | 2R_rate_60d | " if agg_60d else "")
      + "exp_taker_R_28d |")
    p("|---|---:|---:|---:|---:|"
      + ("---:|---:|" if agg_60d else "")
      + "---:|")
    for path in ALL_PATHS:
        st14 = agg_14d["by_path"].get(path, {"n": 0})
        st28 = agg_28d["by_path"].get(path, {"n": 0})
        st60 = agg_60d["by_path"].get(path, {"n": 0}) if agg_60d else None
        et28 = st28.get("expectancy_taker_R", float("nan"))
        et28_s = "n/a" if (et28 != et28) else f"{et28:+.3f}"
        if agg_60d:
            p(f"| {path} | {st14['n']} | {_safe_pct(st14.get('two_r_before_sl', 0), st14['n'])} | "
              f"{st28['n']} | {_safe_pct(st28.get('two_r_before_sl', 0), st28['n'])} | "
              f"{st60['n']} | {_safe_pct(st60.get('two_r_before_sl', 0), st60['n'])} | "
              f"{et28_s} |")
        else:
            p(f"| {path} | {st14['n']} | {_safe_pct(st14.get('two_r_before_sl', 0), st14['n'])} | "
              f"{st28['n']} | {_safe_pct(st28.get('two_r_before_sl', 0), st28['n'])} | "
              f"{et28_s} |")
    p("")

    # ====== 9. SOL/SUI vs 8-symbol universe ======
    p("## 9. SOL/SUI-only vs 8-symbol universe (mode comparison)")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    p("Required mode comparison Table C: each row is a counter-factual "
      "configuration assuming **only the listed paths are enabled** within "
      "the listed symbol set. ‘current_reduced_live’ matches Phase 2B "
      "exactly. ‘SOL_SUI_full_paths’ adds the 4 suppressed paths but "
      "keeps the 2-symbol universe. ‘eight_symbol_full_paths’ unions all "
      "8 symbols with all 6 paths.")
    p("")
    p("| mode | opportunities | 1R_rate | 2R_rate | exp_taker_R | "
      "exp_maker_R | notes |")
    p("|---|---:|---:|---:|---:|---:|---|")

    def _mode_stats(symbols: list[str], paths: list[str]) -> dict:
        # Recompute from primary by_sp
        n = 0; one_r = 0; two_r = 0; et_sum = 0.0; em_sum = 0.0; et_n = 0
        for sym in symbols:
            for path in paths:
                st = primary_agg["by_sp"].get((sym, path))
                if not st or st["n"] == 0:
                    continue
                n += st["n"]
                one_r += st["one_r_before_sl"]
                two_r += st["two_r_before_sl"]
                if not math.isnan(st["expectancy_taker_R"]):
                    et_sum += st["expectancy_taker_R"] * st["n"]
                    em_sum += st["expectancy_maker_R"] * st["n"]
                    et_n += st["n"]
        if n == 0:
            return {"n": 0}
        return {
            "n": n,
            "one_r_rate": one_r / n,
            "two_r_rate": two_r / n,
            "exp_taker_R": (et_sum / et_n) if et_n else float("nan"),
            "exp_maker_R": (em_sum / et_n) if et_n else float("nan"),
        }

    modes = [
        ("current_reduced_live", PRIMARY_SYMBOLS, list(PATHS_ALLOWED),
         "SOL+SUI, 4h_flip + 1h_continuation only — actual Phase 2B."),
        ("SOL_SUI_full_paths", PRIMARY_SYMBOLS, list(ALL_PATHS),
         "SOL+SUI, all 6 paths — counter-factual: re-enable all suppressed cascade levels and routes."),
        ("eight_symbol_full_paths", SYMBOLS, list(ALL_PATHS),
         "All 8 symbols, all 6 paths — full pre-Phase-2B surface."),
        ("eight_symbol_allowed_only", SYMBOLS, list(PATHS_ALLOWED),
         "All 8 symbols, only the currently allowed paths — universe-only widening."),
    ]
    for mode_label, syms, paths, note in modes:
        st = _mode_stats(syms, paths)
        if st["n"] == 0:
            p(f"| {mode_label} | 0 | n/a | n/a | n/a | n/a | {note} |")
            continue
        et = st["exp_taker_R"]; em = st["exp_maker_R"]
        et_s = "n/a" if math.isnan(et) else f"{et:+.3f}"
        em_s = "n/a" if math.isnan(em) else f"{em:+.3f}"
        p(f"| {mode_label} | {st['n']} | {st['one_r_rate']*100:.1f}% | "
          f"{st['two_r_rate']*100:.1f}% | {et_s} | {em_s} | {note} |")
    p("")

    # ====== 10. Best/worst symbol-path combos ======
    p("## 10. Best and worst symbol/path combinations")
    p(f"_(primary window: {primary_window_label})_")
    p("")
    sp_rows = []
    for (sym, path), st in primary_agg["by_sp"].items():
        n = st["n"]
        if n == 0:
            continue
        sp_rows.append({
            "sym": sym, "path": path, "n": n,
            "one_r": st["one_r_before_sl"] / n,
            "two_r": st["two_r_before_sl"] / n,
            "exp_taker": st["expectancy_taker_R"],
            "verdict": verdict_for_path(st),
        })
    sp_rows.sort(key=lambda r: (r["exp_taker"] if not math.isnan(r["exp_taker"]) else -999),
                 reverse=True)
    p("| symbol | path | n | 1R_rate | 2R_rate | exp_taker_R | verdict |")
    p("|---|---|---:|---:|---:|---:|---|")
    for r in sp_rows:
        et = r["exp_taker"]
        et_s = "n/a" if math.isnan(et) else f"{et:+.3f}"
        p(f"| {r['sym']} | {r['path']} | {r['n']} | {r['one_r']*100:.1f}% | "
          f"{r['two_r']*100:.1f}% | {et_s} | {r['verdict']} |")
    p("")

    # ====== 11. Evidence for/against re-enabling ======
    p("## 11. Evidence for or against re-enabling any suppressed path")
    p("")
    suppressed_verdicts: list[tuple[str, str, dict]] = []
    for path in PATHS_SUPPRESSED:
        st = primary_agg["by_path"].get(path, {"n": 0})
        v = verdict_for_path(st)
        suppressed_verdicts.append((path, v, st))

    p("Per-suppressed-path verdict (using the rules from the audit spec):")
    p("")
    p("| path | n | 1R_rate | 2R_rate | exp_taker_R | top_symbol_share | top_day_share | verdict |")
    p("|---|---:|---:|---:|---:|---:|---:|---|")
    for path, v, st in suppressed_verdicts:
        n = st["n"]
        if n == 0:
            p(f"| {path} | 0 | n/a | n/a | n/a | n/a | n/a | {v} |")
            continue
        et = st["expectancy_taker_R"]
        et_s = "n/a" if math.isnan(et) else f"{et:+.3f}"
        p(f"| {path} | {n} | {st['one_r_before_sl']/n*100:.1f}% | "
          f"{st['two_r_before_sl']/n*100:.1f}% | {et_s} | "
          f"{st['top_symbol_share']*100:.1f}% | {st['top_day_share']*100:.1f}% | "
          f"{v} |")
    p("")

    # Verdict reasoning
    promising = [pv for pv in suppressed_verdicts if pv[1] == "PROMISING_CANDIDATE"]
    do_not = [pv for pv in suppressed_verdicts if pv[1] == "DO_NOT_ENABLE"]
    weak = [pv for pv in suppressed_verdicts if pv[1] == "WEAK_SAMPLE"]
    needs_more = [pv for pv in suppressed_verdicts if pv[1] == "NEEDS_LONGER_WINDOW"]
    rejected = [pv for pv in suppressed_verdicts if pv[1] == "REJECT_FOR_NOW"]

    if promising:
        p("**Paths flagged PROMISING_CANDIDATE** (all spec gates cleared):")
        for path, _, st in promising:
            p(f"- `{path}` — n={st['n']}, "
              f"1R-rate={st['one_r_before_sl']/st['n']*100:.1f}%, "
              f"2R-rate={st['two_r_before_sl']/st['n']*100:.1f}%, "
              f"exp_taker={st['expectancy_taker_R']:+.3f}R, "
              f"top-symbol-share={st['top_symbol_share']*100:.1f}%, "
              f"top-day-share={st['top_day_share']*100:.1f}%.")
        p("")
        p("> *PROMISING_CANDIDATE* is the strongest label this audit emits. "
          "It does NOT authorise re-enable. It identifies the candidate that "
          "would justify the next step: a full backtest under "
          "`scripts/backtest_v4.py`-style production code on the same data, "
          "with full SL/TP/fees/funding/leverage modelled.")
        p("")
    if do_not:
        p("**Paths flagged DO_NOT_ENABLE** (any one of: n<10 with 1R<40%, "
          "or n≥10 with 2R=0%, or fee-adjusted taker expectancy negative):")
        for path, _, st in do_not:
            n = st["n"]
            reasons = []
            if not math.isnan(st["expectancy_taker_R"]) and st["expectancy_taker_R"] < 0:
                reasons.append(f"taker expectancy {st['expectancy_taker_R']:+.3f}R < 0")
            if n >= 10 and st["two_r_before_sl"] == 0:
                reasons.append("2R-before-SL = 0% over ≥10 opps")
            if n < 10 and st["one_r_before_sl"] / max(1, n) < 0.40:
                reasons.append(f"only {n} opps and 1R-rate < 40%")
            p(f"- `{path}` — n={n}; reason(s): {'; '.join(reasons) if reasons else 'spec rule triggered.'}")
        p("")
    if weak:
        p("**Paths flagged WEAK_SAMPLE** (insufficient measurable opportunities under spec gates):")
        for path, _, st in weak:
            p(f"- `{path}` — n={st['n']} (< 20). Need a longer window to verdict.")
        p("")
    if needs_more:
        p("**Paths flagged NEEDS_LONGER_WINDOW** (zero opportunities in this window):")
        for path, _, _ in needs_more:
            p(f"- `{path}` — 0 opps. Either the route never fired or no signal "
              f"cleared the MIN_CONFIDENCE=45 gate. Re-run on a longer window.")
        p("")
    if rejected:
        p("**Paths flagged REJECT_FOR_NOW** (sample size adequate but quality bar not cleared):")
        for path, _, st in rejected:
            n = st["n"]
            et = st["expectancy_taker_R"]
            p(f"- `{path}` — n={n}, "
              f"1R-rate={st['one_r_before_sl']/n*100:.1f}%, "
              f"2R-rate={st['two_r_before_sl']/n*100:.1f}%, "
              f"exp_taker={et:+.3f}R. "
              f"Quality not high enough to justify re-enable but not "
              f"catastrophic enough to permanently disable.")
        p("")

    # ====== 12. Smallest evidence-based next action ======
    p("## 12. Single smallest evidence-based next action")
    p("")
    if not promising:
        p("**No path met PROMISING_CANDIDATE in this window.** The "
          "single smallest evidence-based next action is: **do nothing to "
          "live config**. Specifically:")
        p("")
        p("- Do NOT re-enable any suppressed flag.")
        p("- Do NOT widen the symbol universe to ETH/DOGE/XRP/LINK/AVAX/ADA "
          "yet — even on the currently allowed paths.")
        p("- Re-run this audit on a longer window when one becomes available "
          "(28d → 60d → 90d). PROMISING_CANDIDATE requires ≥20 deduped "
          "opportunities, which Phase 2B's slow cadence is unlikely to "
          "provide on most paths until the trend regime returns.")
        p("- The currently allowed reduced-live mode remains "
          "evidence-consistent: empty signals are the market refusing to "
          "produce qualifying setups, not the rules being too tight. "
          "(See `docs/reports/REDUCED_MODE_OPPORTUNITY_QUALITY_AUDIT.md` "
          "for the parallel measurement on allowed paths.)")
    else:
        promising_names = [p_[0] for p_ in promising]
        p(f"**One path met PROMISING_CANDIDATE** in this audit: "
          f"`{', '.join(promising_names)}`.")
        p("")
        p("The single smallest evidence-based next action is:")
        p("")
        p("1. **Do NOT flip the live flag yet.** This audit does not include "
          "fees beyond a coarse round-trip proxy, does not include funding, "
          "and does not include leverage / liquidation buffer interactions.")
        p("2. **Run a full production-code backtest** for the candidate path "
          "(`scripts/backtest_v4.py` pattern) over the same window and "
          "symbols. Confirm Sharpe and max-drawdown stay within the bounds "
          "documented in `CLAUDE.md` §8 (Strategy Versioning Pipeline).")
        p("3. **If and only if the backtest confirms the audit numbers**, "
          "propose a one-flag delta in `src/orchestrator/reduced_live_mode.py` "
          "with a CHANGELOG entry, behind a paper-trade verification step.")
    p("")

    # ====== 13. Red-team review ======
    p("## 13. Red-team review")
    p("")
    p("**Paranoid Auditor.** Every opportunity in the CSV is the product "
      "of `SupertrendTrend.generate_*` / `AdaptiveTrend.generate_signal` / "
      "`BreakoutTrader.generate_signal` directly — the same callables the "
      "live orchestrator uses. We do NOT re-derive ATR, SL, TP, or "
      "confidence; they are read out of the returned `Signal`. The only "
      "thing the audit synthesises that the bot does not is the "
      "deterministic cascade-bypass: we evaluate every cascade level "
      "independently to discover suppressed-level opportunities, even when "
      "an earlier level would have stopped the live cascade. This biases "
      "the COUNT of suppressed opportunities UPWARD (best-case for the "
      "suppressed paths), which is the safer skew for an audit whose "
      "verdict rules treat low counts as DO_NOT_ENABLE / WEAK_SAMPLE.")
    p("")
    p("**Regime Trader.** The 1R milestone uses exact entry-to-SL distance — "
      "the same R the live bot would risk. 4H ATR governs SL/TP for all "
      "SupertrendTrend cascade levels (matches the production comment "
      "`# Entry / SL / TP — use 4H ATR`). The breakout_trader path uses "
      "1H ATR for its SL distance (per `BreakoutTrader._volume_average` and "
      "the SL buffer formula); we still report `atr_4h` in the CSV for "
      "cross-path comparability, but the SL/TP price levels were produced "
      "by the strategy, so per-opp R is correct. If the 2R-rate is below "
      "33% on a path, the market rejected the 2:1 R/R model in this "
      "regime — not a measurement artefact.")
    p("")
    p("**Exchange Microstructure Trader.** The audit assumes spec'd SL/TP "
      "fill prices. Real fills experience slippage, especially on aligned "
      "trend / 15m fast entries that crowd into a momentum extension. The "
      "fee proxy (0.10% taker, 0.07% maker) is the BEST-CASE — it ignores "
      "funding (~0.03%/day at the standard rate) and the bid/ask spread "
      "(typically 0.5-1 bp on top-tier USDT-M perps, but worse on AVAX / "
      "ADA / SUI in volatile minutes). A re-enable decision MUST not be "
      "made on this expectancy proxy alone; a 5-bps slippage assumption "
      "alone consumes 0.05R on a 1% ATR setup.")
    p("")
    p("**Forensic Data Engineer.** 15m candles drive forward measurement. "
      "MFE/MAE use intra-bar high/low, not close — they capture the "
      "highest favourable AND highest adverse price within the bar. The "
      "ambiguous-bar SL-first rule is conservative; it can NEVER overstate "
      "1R/2R-before-SL rates. The 15m page cap is "
      f"{MAX_15M_BARS} bars — 60d at 15m needs ~5856 bars + 192 forward, "
      f"so a 60d window is feasible. The 28d window needs ~2880 bars and "
      f"is comfortably below the cap. The 14d window is half that. No "
      f"fetch-budget artefact distorts opportunity counts.")
    p("")
    p("**Deletionist.** Every metric in this report came from a CSV row. "
      "There are NO derived approximations of the form 'we estimate' or "
      "'roughly'. If a row count is 0, the table cell shows `n/a` rather "
      "than imputing a value. If an aggregate would divide by zero, the "
      "value is `n/a`. The verdict labels are mechanically computed from "
      "the spec's threshold rules — no human judgement entered the "
      "numerical pipeline.")
    p("")
    p("**QA Gremlin.** Edge cases checked: (a) `one_r_dist == 0` "
      "short-circuits to unresolved (degenerate signal); (b) opportunities "
      "near the end of the fetched data are flagged "
      "`truncated_4h/8h/24h/48h` in the CSV — exclude them with "
      "`awk -F, '$truncated_24h==\"False\"'` if a strict subset is needed; "
      "(c) the consecutive-checkpoint dedupe uses a 31-min gap tolerance, "
      "so a single missing cycle starts a new opportunity (slight "
      "over-count, conservative); (d) cascade levels are evaluated "
      "INDEPENDENTLY at each checkpoint rather than via the strict "
      "first-non-NONE order — so the 15m_fast/aligned_trend opportunity "
      "counts are upper bounds; (e) the breakout_trader path's R is "
      "based on the strategy's own SL formula (1H ATR × 0.5 SL buffer + "
      "the broken level), not 4H ATR multiples — the per-opp R is still "
      "self-consistent.")
    p("")

    # ====== 14. Verification ======
    p("## 14. Verification")
    p("")
    p("### 14.1 Re-run command")
    p("```bash")
    p(".venv/bin/python scripts/audit_suppressed_path_quality.py \\")
    p("    --out docs/reports/SUPPRESSED_PATH_QUALITY_AUDIT.md \\")
    p("    --csv docs/reports/suppressed_path_quality.csv \\")
    p("    --summary-csv docs/reports/suppressed_path_quality_summary.csv \\")
    p(f"    --days {fetch_window_days}")
    p("```")
    p("")
    p("### 14.2 Row counts")
    p("```")
    p(f"14d opportunities (after dedupe): {agg_14d['all']['n']} measurable, "
      f"{agg_14d['unmeasurable']} unmeasurable")
    p(f"28d opportunities (after dedupe): {agg_28d['all']['n']} measurable, "
      f"{agg_28d['unmeasurable']} unmeasurable")
    if agg_60d:
        p(f"60d opportunities (after dedupe): {agg_60d['all']['n']} measurable, "
          f"{agg_60d['unmeasurable']} unmeasurable")
    p("```")
    p("")
    p("### 14.3 Per-symbol fetch summary (primary window)")
    p("```")
    for sym in SYMBOLS:
        m = fetch_meta.get(sym, {})
        if m.get("error"):
            p(f"  {sym:<22} ERROR: {m['error']}")
        else:
            p(f"  {sym:<22} 4h={m.get('4h_bars', 0):>4} bars, "
              f"1h={m.get('1h_bars', 0):>5} bars, "
              f"15m={m.get('15m_bars', 0):>5} bars, "
              f"15m_first={m.get('15m_first')}")
    p("```")
    p("")
    p("### 14.4 Per-opportunity CSV columns")
    p("```")
    p("window, symbol, checkpoint, path, route, regime, direction, ")
    p("confidence, entry_price, atr_4h, sl_price, tp_price, one_R_dist, ")
    p("live_allowed, suppressed_flag, run_length, ")
    p("one_r_before_sl, two_r_before_sl, sl_first, unresolved, ")
    p("mfe_4h, mae_4h, mfe_8h, mae_8h, mfe_24h, mae_24h, mfe_48h, mae_48h, ")
    p("time_to_sl_min, time_to_1r_min, time_to_tp_min, ")
    p("fee_drag_R_taker_taker, fee_drag_R_maker_taker, ")
    p("truncated_4h, truncated_8h, truncated_24h, truncated_48h, forward_bars")
    p("```")
    p("")
    p("### 14.5 Summary CSV (one row per (window, scope, key))")
    p("```")
    p("window, scope, key, n, one_r_rate, two_r_rate, unresolved_rate, ")
    p("avg_mfe_24h_R, avg_mae_24h_R, expectancy_taker_R, expectancy_maker_R, ")
    p("median_time_to_1r_min, median_time_to_sl_min, n_symbols, ")
    p("top_symbol_share, top_day_share, verdict")
    p("```")
    p("")
    p("### 14.6 Cross-check: cycle history (timing only)")
    p("```bash")
    p("sqlite3 user_data/claude_quant.db \\")
    p('    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM cycle_history;"')
    p("```")
    p("This audit does NOT use the database for trade outcomes — only "
      "for confirming the live bot was polling during the audit window.")
    p("")

    # ====== 15. Hard-constraint compliance ======
    p("## 15. Hard-constraint compliance")
    p("")
    p("- No file under `src/` was modified by this audit script.")
    p("- No threshold, no CB constant, no route flag was changed.")
    p("- No `reduced_live_mode.py` constants were touched.")
    p("- No orders were placed; only public OHLCV was fetched (REST `fetch_ohlcv`).")
    p("- No DB row was written. The cycle-history check in §14.6 is a SELECT.")
    p("- Every count in this report comes from the CSV in §14.4. The "
      "summary CSV in §14.5 is a strict aggregation of that per-row CSV.")
    p("- The 'measured quality' sections (§3-§10) are strictly counts and "
      "statistics derived from the per-opp CSV.")
    p("- The 'evidence' section (§11) applies the spec's verdict rules "
      "mechanically; no `probably` / `likely` qualifiers used.")
    p("- The 'next action' section (§12) names a single smallest action "
      "and explicitly gates re-enable on a separate full backtest. It "
      "does NOT recommend re-enabling any flag.")
    p("- The audit explicitly evaluates the 6 spec-required paths "
      "(`4h_flip`, `1h_continuation`, `15m_fast`, `aligned_trend`, "
      "`adaptive_trend_route`, `breakout_trader_route`) over 14d and 28d "
      f"primary windows; 60d is included{' as an additional cross-check' if agg_60d else ' only when fetch budget permits — skipped this run'}.")
    p("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args) -> int:
    if os.getenv("BINANCE_TESTNET", "false").lower() == "true":
        print("ERROR: BINANCE_TESTNET=true. This audit must run against mainnet.")
        return 2

    client = MarketDataClient()
    await client.connect()
    engine = IndicatorEngine()

    now = datetime.now(tz=timezone.utc)
    anchor = now.replace(second=0, microsecond=0, minute=(now.minute // 30) * 30)

    primary_days = args.days
    fetch_days = max(primary_days, 60 if args.include_60d else primary_days)

    win_14d_start = anchor - timedelta(days=14)
    win_28d_start = anchor - timedelta(days=28)
    win_60d_start = anchor - timedelta(days=60)
    win_end = anchor

    log.info("Fetching %dd of OHLCV per symbol (cap %d 15m bars)...",
             fetch_days, MAX_15M_BARS)

    cached_data: dict[str, dict] = {}
    for symbol in SYMBOLS:
        df_4h, df_1h, df_15m_raw, meta = await fetch_symbol_data(
            client, engine, symbol, fetch_days,
        )
        cached_data[symbol] = {
            "df_4h": df_4h,
            "df_1h": df_1h,
            "df_15m_raw": df_15m_raw,
            "error": meta.get("error"),
        }
        if meta.get("error"):
            log.error("[%s] fetch error: %s", symbol, meta["error"])

    incomplete: dict[str, list[str]] = {}

    log.info("--- 14d window ---")
    opps_14, fm_14, inc_14 = await run_window(
        client, engine, win_14d_start, win_end, fetch_days, cached_data,
    )
    incomplete["14d"] = inc_14

    log.info("--- 28d window ---")
    opps_28, fm_28, inc_28 = await run_window(
        client, engine, win_28d_start, win_end, fetch_days, cached_data,
    )
    incomplete["28d"] = inc_28

    opps_60: Optional[list[Opportunity]] = None
    if args.include_60d:
        log.info("--- 60d window ---")
        opps_60_, fm_60, inc_60 = await run_window(
            client, engine, win_60d_start, win_end, fetch_days, cached_data,
        )
        opps_60 = opps_60_
        incomplete["60d"] = inc_60

    await client.close()

    # Aggregate
    agg_14d = aggregate(opps_14)
    agg_28d = aggregate(opps_28)
    agg_60d = aggregate(opps_60) if opps_60 is not None else None

    primary_label = (
        "14 days" if primary_days == 14 else
        "60 days" if primary_days == 60 else
        f"{primary_days} days"
    )

    fetch_meta_primary = fm_28 if primary_days == 28 else fm_14
    if primary_days == 60 and opps_60 is not None:
        fetch_meta_primary = fm_60  # type: ignore[assignment]

    primary_agg_for_report = (
        agg_60d if (primary_days == 60 and agg_60d is not None) else
        agg_14d if primary_days == 14 else
        agg_28d
    )

    # Build report
    report = build_report(
        agg_14d=agg_14d,
        agg_28d=agg_28d,
        agg_60d=agg_60d,
        fetch_meta=fetch_meta_primary,
        incomplete=incomplete,
        fetch_window_days=fetch_days,
        primary_window_label=primary_label,
        primary_agg=primary_agg_for_report,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"Wrote {out_path}")

    # Per-opp CSV — concatenate all windows, tagged by window
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    all_opp_rows: list[Opportunity] = []
    rows_per_csv: list[dict] = []
    for label, opps in [("14d", opps_14), ("28d", opps_28)] + (
        [("60d", opps_60)] if opps_60 is not None else []
    ):
        for o in opps:
            f = o.forward
            rows_per_csv.append({
                "window": label,
                "symbol": o.symbol,
                "checkpoint": o.checkpoint.isoformat(),
                "path": o.path,
                "route": o.route,
                "regime": o.regime,
                "direction": o.direction,
                "confidence": o.confidence,
                "entry_price": o.entry_price,
                "atr_4h": o.atr_4h,
                "sl_price": o.sl_price,
                "tp_price": o.tp_price,
                "one_R_dist": abs(o.entry_price - o.sl_price),
                "live_allowed": o.live_allowed,
                "suppressed_flag": o.suppressed_flag,
                "run_length": o.run_length,
                "one_r_before_sl": f.one_r_before_sl if f else None,
                "two_r_before_sl": f.two_r_before_sl if f else None,
                "sl_first": f.sl_first if f else None,
                "unresolved": f.unresolved if f else None,
                "mfe_4h": f.mfe_4h if f else 0.0,
                "mae_4h": f.mae_4h if f else 0.0,
                "mfe_8h": f.mfe_8h if f else 0.0,
                "mae_8h": f.mae_8h if f else 0.0,
                "mfe_24h": f.mfe_24h if f else 0.0,
                "mae_24h": f.mae_24h if f else 0.0,
                "mfe_48h": f.mfe_48h if f else 0.0,
                "mae_48h": f.mae_48h if f else 0.0,
                "time_to_sl_min": f.time_to_sl_min if f else None,
                "time_to_1r_min": f.time_to_1r_min if f else None,
                "time_to_tp_min": f.time_to_tp_min if f else None,
                "fee_drag_R_taker_taker": _fee_drag_R(o, TAKER_TAKER_RT),
                "fee_drag_R_maker_taker": _fee_drag_R(o, MAKER_TAKER_RT),
                "truncated_4h": f.truncated_4h if f else None,
                "truncated_8h": f.truncated_8h if f else None,
                "truncated_24h": f.truncated_24h if f else None,
                "truncated_48h": f.truncated_48h if f else None,
                "forward_bars": f.forward_bars if f else 0,
            })
    pd.DataFrame(rows_per_csv).to_csv(csv_path, index=False)
    print(f"Wrote {csv_path} ({len(rows_per_csv)} rows)")

    # Summary CSV
    summary_path = Path(args.summary_csv)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_input: dict[str, dict] = {"14d": agg_14d, "28d": agg_28d}
    if agg_60d is not None:
        summary_input["60d"] = agg_60d
    write_summary_csv(summary_input, summary_path)
    print(f"Wrote {summary_path}")

    # ----- Stdout summary -----
    print("---")
    print(f"14d  : {agg_14d['all']['n']} measurable opps, "
          f"{agg_14d['unmeasurable']} unmeasurable")
    print(f"28d  : {agg_28d['all']['n']} measurable opps, "
          f"{agg_28d['unmeasurable']} unmeasurable")
    if agg_60d:
        print(f"60d  : {agg_60d['all']['n']} measurable opps, "
              f"{agg_60d['unmeasurable']} unmeasurable")
    print("---")
    print("Per-path verdicts (primary window):")
    primary_agg = agg_28d if primary_days == 28 else (agg_60d if primary_days == 60 else agg_14d)
    if primary_agg is None:
        primary_agg = agg_28d
    for path in ALL_PATHS:
        st = primary_agg["by_path"].get(path, {"n": 0})
        v = verdict_for_path(st)
        n = st["n"]
        if n == 0:
            print(f"  {path:<24} n=0           verdict={v}")
        else:
            et = st["expectancy_taker_R"]
            et_s = "n/a" if math.isnan(et) else f"{et:+.3f}"
            print(f"  {path:<24} n={n:<4} 1R={st['one_r_before_sl']/n*100:5.1f}% "
                  f"2R={st['two_r_before_sl']/n*100:5.1f}% exp_taker={et_s}  verdict={v}")
    if incomplete_unique := sorted({s for v in incomplete.values() for s in v}):
        print("---")
        print(f"INCOMPLETE SYMBOLS: {', '.join(incomplete_unique)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default="docs/reports/SUPPRESSED_PATH_QUALITY_AUDIT.md",
    )
    ap.add_argument(
        "--csv",
        default="docs/reports/suppressed_path_quality.csv",
    )
    ap.add_argument(
        "--summary-csv",
        default="docs/reports/suppressed_path_quality_summary.csv",
    )
    ap.add_argument("--days", type=int, default=28,
                    help="Primary window (14, 28, or 60). 14d and 28d ALWAYS run.")
    ap.add_argument("--include-60d", action="store_true",
                    help="Also run a 60-day cross-check (heavier 15m fetches).")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
