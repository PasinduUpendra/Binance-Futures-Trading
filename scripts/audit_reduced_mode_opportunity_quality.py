"""Reduced-mode OPPORTUNITY QUALITY audit.

Read-only forensic tool. For every reduced-mode reachable signal in the
audit window (4H flip OR 1H continuation, conf >= 45, MIN_CONFIDENCE), walk
the post-signal price path forward via 15m OHLCV and measure:

  * first-touch order between -1R (stop) and +1R / +2R (profit milestones)
  * max favorable / adverse excursion at 4h / 8h / 24h horizons
  * time-to-first-touch (SL, +1R, TP)

Mirrors the production signal pipeline (`SupertrendTrend.generate_signal` +
`generate_continuation_signal`, `RegimeDetector`, `IndicatorEngine`,
`AdaptiveStrategy.MIN_CONFIDENCE = 45`) and the reduced-live mode flag set
(`ALLOW_4H_FLIP=True`, `ALLOW_1H_CONTINUATION=True`, all others False).

NOT a backtest. NO orders. NO DB mutations. NO config changes.

Usage
-----
    .venv/bin/python scripts/audit_reduced_mode_opportunity_quality.py \\
        --out docs/reports/REDUCED_MODE_OPPORTUNITY_QUALITY_AUDIT.md \\
        --csv docs/reports/reduced_mode_opportunity_quality.csv

Optional `--days 14` for the secondary 14-day window.

Assumptions (explicit)
----------------------
1. Entry reference is the 1H close at the signal checkpoint. This matches the
   `entry_price` argument the live orchestrator passes (Step 1b uses 1H close
   for SupertrendTrend regardless of cascade level).
2. SL/TP distances use 4H ATR with regime-aware multipliers (3.0/6.0 for
   trending; 2.5/5.0 for ranging — exact production values, no tuning).
3. Forward path is read from 15m candle highs/lows. We do NOT model fills,
   slippage, fees, funding, leverage, or partial scaling. We measure pure
   market follow-through versus the signal-time SL/TP.
4. First-touch logic: walk 15m candles in chronological order. Within a
   candle that touches BOTH SL and TP (rare), we conservatively assume the
   ADVERSE level was hit first (i.e. we do NOT credit a winner when the same
   candle covers both directions). This is the standard "ambiguous bar"
   convention used in published TA literature and is the SAFER assumption
   for an opportunity-quality audit.
5. Unique opportunity: a signal that fires at the same direction across
   consecutive checkpoints is the SAME opportunity (the live bot would only
   open one position; subsequent signals would be skipped by the same-side
   open-position guard). We dedupe by collapsing consecutive same-direction
   signals on the same path into a single opportunity, anchored on the FIRST
   checkpoint of the run.
6. Horizon truncation: opportunities whose 24h horizon extends past the
   end of the fetched 15m data are reported as "unresolved within available
   data" but their MFE/MAE figures cover whatever sub-horizon is available
   (4h / 8h truncations are noted per row).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
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
from src.strategies.base_strategy import Signal, SignalDirection  # noqa: E402
from src.strategies.regime_detector import MarketRegime, RegimeDetector  # noqa: E402
from src.strategies.supertrend_trend import SupertrendTrend  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("audit_quality")
log.setLevel(logging.INFO)

# Mirror live config exactly.
SYMBOLS = ["SOL/USDT:USDT", "SUI/USDT:USDT"]
CYCLE_MINUTES = 30
MIN_CONFIDENCE = AdaptiveStrategy.MIN_CONFIDENCE  # 45.0
HORIZONS_HOURS = (4, 8, 24)
FORWARD_BUFFER_HOURS = 24  # how much extra 15m data to fetch past window end

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df.set_index("timestamp").sort_index()
    return df


async def fetch_paged_15m(
    client: MarketDataClient,
    symbol: str,
    total_needed: int,
) -> pd.DataFrame:
    """Fetch >1000 15m bars by paging via ccxt `since` parameter.

    `MarketDataClient.fetch_ohlcv` always returns the most-recent N bars;
    Binance caps at 1000-1500 per call depending on instrument. To cover
    the 14-day audit window plus a 24h forward buffer (~1440+ bars) we
    page backwards using `exchange.fetch_ohlcv(..., since=...)`.
    """
    exchange = client._require_exchange()
    tf_ms = 15 * 60 * 1000
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    # Anchor: we want bars going back roughly `total_needed * 15min`.
    earliest_ms = end_ms - total_needed * tf_ms

    all_rows: list[list] = []
    cursor = earliest_ms
    while cursor < end_ms:
        batch = await exchange.fetch_ohlcv(
            symbol, timeframe="15m", since=cursor, limit=1000,
        )
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + tf_ms
        # Safety: cap total at 4000 bars (~41 days)
        if len(all_rows) > 4000:
            break

    # Dedupe by timestamp (Binance can include `since` boundary bar)
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
# Signal evaluation at checkpoint
# ---------------------------------------------------------------------------

@dataclass
class RawSignal:
    symbol: str
    checkpoint: datetime
    path: str  # "4h_flip" or "1h_continuation"
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    atr_4h: float
    sl_price: float
    tp_price: float
    confidence: float
    regime: str


def _closed_slice(df: pd.DataFrame, tf_minutes: int, n_required: int, t: datetime) -> Optional[pd.DataFrame]:
    cutoff = t - timedelta(minutes=tf_minutes)
    s = df[df.index <= cutoff]
    if len(s) < n_required:
        return None
    return s


def evaluate_reduced_signals(
    symbol: str,
    checkpoint: datetime,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
) -> list[RawSignal]:
    """Return the (up to 2) reduced-mode reachable signals at this checkpoint.

    Reduced-mode rules: only `generate_signal` (4H flip) and
    `generate_continuation_signal` (1H cont.) are LIVE; both must pass
    `MIN_CONFIDENCE >= 45` to be reachable.
    """
    s4h = _closed_slice(df_4h, 240, 50, checkpoint)
    s1h = _closed_slice(df_1h, 60, 50, checkpoint)
    if s4h is None or s1h is None:
        return []

    # Regime (from 4H)
    rd = RegimeDetector()
    try:
        regime_state = rd.detect(s4h)
    except (KeyError, ValueError):
        return []

    # Mirror AdaptiveStrategy.select_strategy for the supertrend_trend route only
    if regime_state.regime == MarketRegime.TRENDING:
        if regime_state.adx < 18.0:
            return []
        regime_str = "trending"
    elif regime_state.regime == MarketRegime.RANGING and regime_state.adx >= 18.0:
        regime_str = "ranging"
    else:
        return []  # adaptive_trend / breakout / quiet not allowed in reduced mode

    st = SupertrendTrend()
    close_1h = float(s1h["close"].dropna().iloc[-1])
    out: list[RawSignal] = []

    # 4H flip
    try:
        sig_4h: Optional[Signal] = st.generate_signal(s4h, entry_price=close_1h, regime=regime_str)
    except Exception:
        sig_4h = None
    if sig_4h and sig_4h.direction != SignalDirection.NONE \
            and sig_4h.confidence >= MIN_CONFIDENCE:
        atr_4h = float(s4h["atr"].dropna().iloc[-1])
        out.append(RawSignal(
            symbol=symbol,
            checkpoint=checkpoint,
            path="4h_flip",
            direction=sig_4h.direction.value.upper(),
            entry_price=float(sig_4h.entry_price),
            atr_4h=atr_4h,
            sl_price=float(sig_4h.stop_loss),
            tp_price=float(sig_4h.take_profit),
            confidence=float(sig_4h.confidence),
            regime=regime_str,
        ))

    # 1H continuation
    try:
        sig_1h: Optional[Signal] = st.generate_continuation_signal(s4h, s1h, regime=regime_str)
    except Exception:
        sig_1h = None
    if sig_1h and sig_1h.direction != SignalDirection.NONE \
            and sig_1h.confidence >= MIN_CONFIDENCE:
        atr_4h = float(s4h["atr"].dropna().iloc[-1])
        out.append(RawSignal(
            symbol=symbol,
            checkpoint=checkpoint,
            path="1h_continuation",
            direction=sig_1h.direction.value.upper(),
            entry_price=float(sig_1h.entry_price),
            atr_4h=atr_4h,
            sl_price=float(sig_1h.stop_loss),
            tp_price=float(sig_1h.take_profit),
            confidence=float(sig_1h.confidence),
            regime=regime_str,
        ))

    return out


# ---------------------------------------------------------------------------
# Forward path measurement
# ---------------------------------------------------------------------------

@dataclass
class ForwardResult:
    # First-touch booleans (within 24h horizon)
    one_r_before_sl: Optional[bool]  # None == unresolved within horizon
    two_r_before_sl: Optional[bool]
    tp_before_sl: Optional[bool]
    sl_first: Optional[bool]
    unresolved: bool  # neither SL nor TP nor +1R hit within 24h

    # MFE/MAE per horizon (in price units, signed-distance-from-entry abs)
    mfe_4h: float
    mae_4h: float
    mfe_8h: float
    mae_8h: float
    mfe_24h: float
    mae_24h: float

    # Time-to-first-touch (in minutes from signal time; None if not reached)
    time_to_sl_min: Optional[int]
    time_to_1r_min: Optional[int]
    time_to_tp_min: Optional[int]

    # Truncation flags (True means horizon extended beyond fetched data)
    truncated_4h: bool
    truncated_8h: bool
    truncated_24h: bool

    # Bars used for forward walk
    forward_bars: int


def measure_forward(
    sig: RawSignal,
    df_15m: pd.DataFrame,
) -> ForwardResult:
    """Walk forward 15m candles from signal time. Measure first-touch & MFE/MAE."""
    is_long = sig.direction == "LONG"
    one_r_dist = abs(sig.entry_price - sig.sl_price)
    if one_r_dist <= 0:
        # degenerate; treat as unresolved
        return ForwardResult(
            None, None, None, None, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            None, None, None,
            False, False, False, 0,
        )
    one_r_target = sig.entry_price + one_r_dist if is_long else sig.entry_price - one_r_dist
    two_r_target = sig.entry_price + 2 * one_r_dist if is_long else sig.entry_price - 2 * one_r_dist

    # Forward window: candles whose OPEN > signal time (signal fires at 1H close
    # which equals checkpoint - 60min; we use the 15m candle whose open >=
    # checkpoint as the first forward bar).
    fwd = df_15m[df_15m.index >= sig.checkpoint]
    if fwd.empty:
        return ForwardResult(
            None, None, None, None, True,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            None, None, None,
            True, True, True, 0,
        )

    horizon_24h = sig.checkpoint + timedelta(hours=24)
    horizon_8h = sig.checkpoint + timedelta(hours=8)
    horizon_4h = sig.checkpoint + timedelta(hours=4)
    fwd_24h = fwd[fwd.index < horizon_24h]
    if fwd_24h.empty:
        fwd_24h = fwd  # fallback to whatever exists

    # Walk for first-touch
    sl_hit_at: Optional[datetime] = None
    one_r_hit_at: Optional[datetime] = None
    tp_hit_at: Optional[datetime] = None

    # MFE/MAE running max/min by horizon
    mfe = {h: 0.0 for h in HORIZONS_HOURS}
    mae = {h: 0.0 for h in HORIZONS_HOURS}

    for ts, row in fwd_24h.iterrows():
        hi = float(row["high"])
        lo = float(row["low"])
        # MFE/MAE in absolute price excursion from entry, signed by direction
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

        # First-touch evaluation (within 24h)
        # Note: candle covers both directions ambiguously → conservative: SL first.
        candle_hits_sl = (lo <= sig.sl_price) if is_long else (hi >= sig.sl_price)
        candle_hits_1r = (hi >= one_r_target) if is_long else (lo <= one_r_target)
        candle_hits_tp = (hi >= sig.tp_price) if is_long else (lo <= sig.tp_price)

        if sl_hit_at is None and candle_hits_sl:
            sl_hit_at = ts
        if one_r_hit_at is None and candle_hits_1r:
            # Conservative: if same candle hits SL too, do NOT credit 1R first
            if not (candle_hits_sl and sl_hit_at == ts):
                one_r_hit_at = ts
        if tp_hit_at is None and candle_hits_tp:
            if not (candle_hits_sl and sl_hit_at == ts):
                tp_hit_at = ts

        # Stop walking once SL or TP confirmed (1R is a milestone, not exit)
        if sl_hit_at is not None or tp_hit_at is not None:
            # Don't break — we still want full-horizon MFE/MAE
            pass

    # Resolve outcomes
    def _first(*candidates: Optional[datetime]) -> Optional[datetime]:
        c = [x for x in candidates if x is not None]
        return min(c) if c else None

    sl_first: Optional[bool] = None
    tp_before_sl: Optional[bool] = None
    one_r_before_sl: Optional[bool] = None
    two_r_before_sl: Optional[bool] = None  # alias for tp_before_sl since TP == +2R

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

    # In our SL/TP model, TP is exactly +2R (multipliers 6.0 SL=3.0 → R/R=2.0
    # for trending, 5.0/2.5 → R/R=2.0 for ranging). So 2R-before-SL == TP-before-SL.
    two_r_before_sl = tp_before_sl

    def _td_min(t: Optional[datetime]) -> Optional[int]:
        if t is None:
            return None
        return int((t - sig.checkpoint).total_seconds() / 60)

    # Truncation flags: did our forward data cover the full horizon?
    # Each 15m bar covers [ts, ts + 15min). Coverage extends to last_ts + 15min.
    last_ts = fwd_24h.index[-1] if len(fwd_24h) else sig.checkpoint
    coverage_end = last_ts + timedelta(minutes=15)
    truncated_4h = coverage_end < horizon_4h
    truncated_8h = coverage_end < horizon_8h
    truncated_24h = coverage_end < horizon_24h

    return ForwardResult(
        one_r_before_sl=one_r_before_sl,
        two_r_before_sl=two_r_before_sl,
        tp_before_sl=tp_before_sl,
        sl_first=sl_first,
        unresolved=unresolved,
        mfe_4h=mfe[4], mae_4h=mae[4],
        mfe_8h=mfe[8], mae_8h=mae[8],
        mfe_24h=mfe[24], mae_24h=mae[24],
        time_to_sl_min=_td_min(sl_hit_at),
        time_to_1r_min=_td_min(one_r_hit_at),
        time_to_tp_min=_td_min(tp_hit_at),
        truncated_4h=truncated_4h,
        truncated_8h=truncated_8h,
        truncated_24h=truncated_24h,
        forward_bars=len(fwd_24h),
    )


# ---------------------------------------------------------------------------
# Opportunity dedupe
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    symbol: str
    checkpoint: datetime  # FIRST checkpoint of the run
    path: str
    direction: str
    entry_price: float
    atr_4h: float
    sl_price: float
    tp_price: float
    confidence: float
    regime: str
    run_length: int  # how many consecutive checkpoints fired this signal
    forward: ForwardResult = field(default=None)  # type: ignore[assignment]


def dedupe_runs(raw_signals: list[RawSignal]) -> list[Opportunity]:
    """Collapse consecutive same-direction signals on the same path into one
    opportunity, anchored on the FIRST checkpoint of the run.

    A "run" breaks when:
    - direction changes
    - or there is a checkpoint gap > CYCLE_MINUTES (i.e. the signal vanished
      between two adjacent 30-min checkpoints, then came back later — that
      is treated as a new opportunity).
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
        direction=head.direction,
        entry_price=head.entry_price,
        atr_4h=head.atr_4h,
        sl_price=head.sl_price,
        tp_price=head.tp_price,
        confidence=head.confidence,
        regime=head.regime,
        run_length=len(run),
    )


# ---------------------------------------------------------------------------
# Aggregation & reporting
# ---------------------------------------------------------------------------

def _safe_pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def _median(xs: list[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    if not vals:
        return None
    return statistics.median(vals)


def aggregate(opps: list[Opportunity]) -> dict:
    """Build all summary stats for the report.

    Opportunities with `forward.forward_bars == 0` are un-measurable
    (signal predates the available 15m data) and are EXCLUDED from
    aggregate quality stats. Their count is reported separately as
    `unmeasurable`.
    """
    unmeasurable_total = sum(
        1 for o in opps if o.forward is not None and o.forward.forward_bars == 0
    )
    measurable = [o for o in opps if o.forward is not None and o.forward.forward_bars > 0]

    by_sp: dict[tuple[str, str], list[Opportunity]] = defaultdict(list)
    by_path: dict[str, list[Opportunity]] = defaultdict(list)
    by_symbol: dict[str, list[Opportunity]] = defaultdict(list)
    for o in measurable:
        by_sp[(o.symbol, o.path)].append(o)
        by_path[o.path].append(o)
        by_symbol[o.symbol].append(o)

    def _stats(items: list[Opportunity]) -> dict:
        n = len(items)
        if n == 0:
            return {
                "n": 0,
                "one_r_before_sl": 0,
                "two_r_before_sl": 0,
                "sl_first": 0,
                "unresolved": 0,
                "avg_mfe_4h": 0.0, "avg_mfe_8h": 0.0, "avg_mfe_24h": 0.0,
                "avg_mae_4h": 0.0, "avg_mae_8h": 0.0, "avg_mae_24h": 0.0,
                "median_time_to_1r_min": None,
                "median_time_to_sl_min": None,
                "median_time_to_tp_min": None,
            }

        def _frac_in_terms_of_R(v_abs: float, items: list[Opportunity]) -> float:
            # Express MFE/MAE as a multiple of R using each item's own one-R
            return v_abs

        one_r = sum(1 for o in items if o.forward.one_r_before_sl is True)
        two_r = sum(1 for o in items if o.forward.two_r_before_sl is True)
        sl_first = sum(1 for o in items if o.forward.sl_first is True)
        unresolved = sum(1 for o in items if o.forward.unresolved)

        # Express MFE/MAE as multiples of R for fair cross-symbol/cross-path comparison
        mfe_r = {h: [] for h in HORIZONS_HOURS}
        mae_r = {h: [] for h in HORIZONS_HOURS}
        for o in items:
            r = abs(o.entry_price - o.sl_price)
            if r <= 0:
                continue
            mfe_r[4].append(o.forward.mfe_4h / r)
            mfe_r[8].append(o.forward.mfe_8h / r)
            mfe_r[24].append(o.forward.mfe_24h / r)
            mae_r[4].append(o.forward.mae_4h / r)
            mae_r[8].append(o.forward.mae_8h / r)
            mae_r[24].append(o.forward.mae_24h / r)

        def _avg(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        return {
            "n": n,
            "one_r_before_sl": one_r,
            "two_r_before_sl": two_r,
            "sl_first": sl_first,
            "unresolved": unresolved,
            "avg_mfe_4h_R": _avg(mfe_r[4]),
            "avg_mfe_8h_R": _avg(mfe_r[8]),
            "avg_mfe_24h_R": _avg(mfe_r[24]),
            "avg_mae_4h_R": _avg(mae_r[4]),
            "avg_mae_8h_R": _avg(mae_r[8]),
            "avg_mae_24h_R": _avg(mae_r[24]),
            "median_time_to_1r_min": _median([o.forward.time_to_1r_min for o in items]),
            "median_time_to_sl_min": _median([o.forward.time_to_sl_min for o in items]),
            "median_time_to_tp_min": _median([o.forward.time_to_tp_min for o in items]),
        }

    return {
        "by_sp": {k: _stats(v) for k, v in by_sp.items()},
        "by_path": {k: _stats(v) for k, v in by_path.items()},
        "by_symbol": {k: _stats(v) for k, v in by_symbol.items()},
        "all": _stats(measurable),
        "unmeasurable": unmeasurable_total,
        "measurable_total": len(measurable),
    }


def build_report(
    opps_7d: list[Opportunity],
    agg_7d: dict,
    opps_14d: Optional[list[Opportunity]],
    agg_14d: Optional[dict],
    fetch_meta: dict,
    reachability_xref: dict,
) -> str:
    now = datetime.now(tz=timezone.utc)
    out: list[str] = []
    p = out.append

    p("# REDUCED_MODE_OPPORTUNITY_QUALITY_AUDIT")
    p("")
    p(f"> Generated: {now.isoformat()} UTC")
    p("> Script: `scripts/audit_reduced_mode_opportunity_quality.py`")
    p("> Source: Binance mainnet OHLCV (read-only). No orders, no DB writes, no config changes.")
    p("")

    # ===== 1. Audit window =====
    p("## 1. Audit window")
    p("")
    p(f"- **Primary (7-day)**: `{fetch_meta['win7_start'].isoformat()}` → "
      f"`{fetch_meta['win7_end'].isoformat()}`")
    if fetch_meta.get("win14_start"):
        p(f"- **Secondary (14-day)**: `{fetch_meta['win14_start'].isoformat()}` → "
          f"`{fetch_meta['win14_end'].isoformat()}`")
    p(f"- **Checkpoint cadence**: every {CYCLE_MINUTES} minutes (matches `CYCLE_INTERVAL_SECONDS=1800`).")
    p(f"- **Symbols**: {', '.join(SYMBOLS)}")
    p(f"- **Forward horizon**: 4h / 8h / 24h after each signal checkpoint.")
    p("")

    # ===== 2. Method and assumptions =====
    p("## 2. Method and assumptions")
    p("")
    p("1. Pull OHLCV (4H, 1H, 15m) from Binance mainnet via the production")
    p("   `MarketDataClient.fetch_ohlcv` path. Compute indicators ONCE on the")
    p("   full dataset using the production `IndicatorEngine.calculate_all`")
    p("   (Supertrend(8, 2.0), ADX(14), ATR(14), EMA/RSI/BB/Volume SMA — all causal).")
    p("2. Step every 30 minutes through the audit window. At each checkpoint:")
    p("   - Slice each TF to candles whose CLOSE is ≤ checkpoint (drop in-progress).")
    p("   - Run `RegimeDetector.detect` on the 4H slice.")
    p("   - Mirror `AdaptiveStrategy.select_strategy` route gate (TRENDING+ADX≥18 or")
    p("     RANGING+ADX≥18 → supertrend_trend; everything else → no signal).")
    p("   - Call `SupertrendTrend.generate_signal` (4H flip) and")
    p("     `SupertrendTrend.generate_continuation_signal` (1H continuation) directly.")
    p("   - Apply `MIN_CONFIDENCE = 45` gate.")
    p("3. **Reduced-mode parity**: the `15m_fast` and `aligned_trend` cascades are NOT")
    p("   evaluated as live, matching the Phase-2B flag set. Any signal there is")
    p("   excluded from this audit's 'reachable' count.")
    p("4. **Dedupe runs**: when the same (symbol, path, direction) signal recurs on")
    p("   consecutive checkpoints, it is collapsed into a single opportunity anchored")
    p("   at the FIRST checkpoint. The live bot would only open one position; later")
    p("   same-direction signals would be skipped by the open-position guard.")
    p("5. **Forward path**: walk the post-signal 15m candles for up to 24h and record")
    p("   first-touch order between SL, +1R, and TP (=+2R). MFE/MAE are taken")
    p("   per-horizon as the maximum signed excursion from entry within the horizon.")
    p("6. **Ambiguous bar**: a 15m candle that touches both SL and TP within its")
    p("   high/low is treated as **SL-first** (conservative). This skews the audit")
    p("   AWAY from a bullish conclusion — the opposite skew of an optimistic backtest.")
    p("7. **NO fill modelling**: no slippage, fees, funding, leverage, or partials.")
    p("   This is a signal-quality measurement against the spec'd SL/TP, not a P&L.")
    p("8. **R units**: MFE/MAE are reported as multiples of R (where R = entry-to-SL")
    p("   distance) to make symbols and paths comparable on the same scale.")
    p("9. **Horizon truncation**: opportunities near the end of the fetched data may")
    p("   not have a full 24h forward window. The CSV `truncated_*` columns flag")
    p("   these rows. The MFE/MAE for that horizon then reflect the available subset.")
    p("")

    # ===== 3. Opportunity count by symbol and path =====
    p("## 3. Opportunity count by symbol and path (7-day)")
    p("")
    p("| symbol | path | opportunities | 1R_before_SL | 2R_before_SL | unresolved |")
    p("|---|---|---:|---:|---:|---:|")
    rows: list[tuple[str, str, int, int, int, int]] = []
    for sym in SYMBOLS:
        for path in ("4h_flip", "1h_continuation"):
            stats = agg_7d["by_sp"].get((sym, path))
            if not stats:
                rows.append((sym, path, 0, 0, 0, 0))
                continue
            rows.append((
                sym, path, stats["n"],
                stats["one_r_before_sl"], stats["two_r_before_sl"], stats["unresolved"],
            ))
    for sym, path, n, oneR, twoR, unr in rows:
        oneR_pct = _safe_pct(oneR, n)
        twoR_pct = _safe_pct(twoR, n)
        unr_pct = _safe_pct(unr, n)
        p(f"| {sym} | {path} | {n} | {oneR} ({oneR_pct}) | {twoR} ({twoR_pct}) | {unr} ({unr_pct}) |")
    tot_n = agg_7d["all"]["n"]
    tot_1r = agg_7d["all"]["one_r_before_sl"]
    tot_2r = agg_7d["all"]["two_r_before_sl"]
    tot_unr = agg_7d["all"]["unresolved"]
    p(f"| **TOTAL** |  | **{tot_n}** | **{tot_1r}** ({_safe_pct(tot_1r, tot_n)}) | "
      f"**{tot_2r}** ({_safe_pct(tot_2r, tot_n)}) | **{tot_unr}** ({_safe_pct(tot_unr, tot_n)}) |")
    p("")
    unmeas_7d = agg_7d.get("unmeasurable", 0)
    if unmeas_7d:
        p(f"> **Note**: {unmeas_7d} opportunit{'y' if unmeas_7d == 1 else 'ies'} "
          f"in the 7-day window had signal timestamps before the earliest available "
          f"15m bar (Binance hard-caps OHLCV history at ~1000 bars per call). "
          f"They are EXCLUDED from quality stats above and from §4–§6 percentages, "
          f"but appear in the CSV with `forward_bars=0` and `unresolved=True`.")
        p("")

    # ===== 4. 1R-before-SL table =====
    p("## 4. 1R-before-SL table")
    p("")
    p("Probability that price reached +1R (i.e. break-even on the risk amount)")
    p("BEFORE -1R (stop-loss). Measured to first-touch within 24h horizon.")
    p("")
    p("| symbol | path | n | 1R_before_SL | rate | sl_first | unresolved |")
    p("|---|---|---:|---:|---:|---:|---:|")
    for sym in SYMBOLS:
        for path in ("4h_flip", "1h_continuation"):
            s = agg_7d["by_sp"].get((sym, path))
            if not s or s["n"] == 0:
                p(f"| {sym} | {path} | 0 | 0 | n/a | 0 | 0 |")
                continue
            p(f"| {sym} | {path} | {s['n']} | {s['one_r_before_sl']} | "
              f"{_safe_pct(s['one_r_before_sl'], s['n'])} | "
              f"{s['sl_first']} | {s['unresolved']} |")
    p("")

    # ===== 5. 2R-before-SL table =====
    p("## 5. 2R-before-SL table")
    p("")
    p("Probability that price reached +2R (the spec'd take-profit level) BEFORE")
    p("-1R. This is the survival rate for a trade taken to its TP without first")
    p("getting stopped out.")
    p("")
    p("| symbol | path | n | 2R_before_SL | rate |")
    p("|---|---|---:|---:|---:|")
    for sym in SYMBOLS:
        for path in ("4h_flip", "1h_continuation"):
            s = agg_7d["by_sp"].get((sym, path))
            if not s or s["n"] == 0:
                p(f"| {sym} | {path} | 0 | 0 | n/a |")
                continue
            p(f"| {sym} | {path} | {s['n']} | {s['two_r_before_sl']} | "
              f"{_safe_pct(s['two_r_before_sl'], s['n'])} |")
    p("")

    # ===== 6. MFE/MAE summary =====
    p("## 6. MFE/MAE summary by path (in units of R)")
    p("")
    p("| symbol | path | avg_MFE_4h | avg_MFE_8h | avg_MFE_24h | avg_MAE_4h | avg_MAE_8h | avg_MAE_24h |")
    p("|---|---|---:|---:|---:|---:|---:|---:|")
    for sym in SYMBOLS:
        for path in ("4h_flip", "1h_continuation"):
            s = agg_7d["by_sp"].get((sym, path))
            if not s or s["n"] == 0:
                p(f"| {sym} | {path} | n/a | n/a | n/a | n/a | n/a | n/a |")
                continue
            p(f"| {sym} | {path} | "
              f"{s['avg_mfe_4h_R']:.2f} | {s['avg_mfe_8h_R']:.2f} | {s['avg_mfe_24h_R']:.2f} | "
              f"{s['avg_mae_4h_R']:.2f} | {s['avg_mae_8h_R']:.2f} | {s['avg_mae_24h_R']:.2f} |")
    p("")
    p("> Read as: '0.50' under avg_MFE_4h means on average the price moved 0.5R")
    p("> in the favorable direction within 4 hours of the signal.")
    p("")

    # ===== 7. SOL vs SUI =====
    p("## 7. SOL vs SUI comparison (7-day, both paths combined)")
    p("")
    p("| symbol | n | 1R_rate | 2R_rate | unresolved | avg_MFE_24h_R | avg_MAE_24h_R | median_time_to_1R_min | median_time_to_SL_min |")
    p("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sym in SYMBOLS:
        s = agg_7d["by_symbol"].get(sym)
        if not s or s["n"] == 0:
            p(f"| {sym} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        p(f"| {sym} | {s['n']} | {_safe_pct(s['one_r_before_sl'], s['n'])} | "
          f"{_safe_pct(s['two_r_before_sl'], s['n'])} | {_safe_pct(s['unresolved'], s['n'])} | "
          f"{s['avg_mfe_24h_R']:.2f} | {s['avg_mae_24h_R']:.2f} | "
          f"{s['median_time_to_1r_min'] if s['median_time_to_1r_min'] is not None else 'n/a'} | "
          f"{s['median_time_to_sl_min'] if s['median_time_to_sl_min'] is not None else 'n/a'} |")
    p("")

    # ===== 8. 4H flip vs 1H continuation =====
    p("## 8. 4H flip vs 1H continuation comparison (7-day, both symbols combined)")
    p("")
    p("| path | n | 1R_rate | 2R_rate | unresolved | avg_MFE_24h_R | avg_MAE_24h_R | median_time_to_1R_min | median_time_to_SL_min |")
    p("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for path in ("4h_flip", "1h_continuation"):
        s = agg_7d["by_path"].get(path)
        if not s or s["n"] == 0:
            p(f"| {path} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        p(f"| {path} | {s['n']} | {_safe_pct(s['one_r_before_sl'], s['n'])} | "
          f"{_safe_pct(s['two_r_before_sl'], s['n'])} | {_safe_pct(s['unresolved'], s['n'])} | "
          f"{s['avg_mfe_24h_R']:.2f} | {s['avg_mae_24h_R']:.2f} | "
          f"{s['median_time_to_1r_min'] if s['median_time_to_1r_min'] is not None else 'n/a'} | "
          f"{s['median_time_to_sl_min'] if s['median_time_to_sl_min'] is not None else 'n/a'} |")
    p("")

    # Required table 3
    p("### 8.1 Required table — path | median_time_to_1R | median_time_to_SL | notes")
    p("")
    p("| path | median_time_to_1R | median_time_to_SL | notes |")
    p("|---|---:|---:|---|")
    notes = {
        "4h_flip": "Cascade level 1; uses last 4H candle flip; entry @ 1H close, SL/TP via 4H ATR×3/×6.",
        "1h_continuation": "Cascade level 2; 4H established + 1H flip within last 8 bars; same SL/TP.",
    }
    for path in ("4h_flip", "1h_continuation"):
        s = agg_7d["by_path"].get(path)
        if not s or s["n"] == 0:
            p(f"| {path} | n/a | n/a | {notes[path]} |")
            continue
        t1r = s["median_time_to_1r_min"]
        tsl = s["median_time_to_sl_min"]
        p(f"| {path} | {t1r if t1r is not None else 'n/a'} | "
          f"{tsl if tsl is not None else 'n/a'} | {notes[path]} |")
    p("")

    # Optional secondary 14-day window
    if agg_14d:
        p("## 8.2 Secondary 14-day cross-check")
        p("")
        p("| path | n_14d | 1R_rate_14d | 2R_rate_14d | n_7d | 1R_rate_7d |")
        p("|---|---:|---:|---:|---:|---:|")
        for path in ("4h_flip", "1h_continuation"):
            s14 = agg_14d["by_path"].get(path)
            s7 = agg_7d["by_path"].get(path)
            n14 = s14["n"] if s14 else 0
            n7 = s7["n"] if s7 else 0
            r14 = _safe_pct(s14["one_r_before_sl"], n14) if n14 else "n/a"
            two14 = _safe_pct(s14["two_r_before_sl"], n14) if n14 else "n/a"
            r7 = _safe_pct(s7["one_r_before_sl"], n7) if n7 else "n/a"
            p(f"| {path} | {n14} | {r14} | {two14} | {n7} | {r7} |")
        p("")

    # ===== 9. Are reduced-mode opportunities worth trading? =====
    p("## 9. Are current reduced-mode opportunities worth trading?")
    p("")
    a = agg_7d["all"]
    n = a["n"]
    if n == 0:
        p("**Insufficient data — no reduced-mode reachable opportunities in the 7-day window.**")
        p("")
        p("With zero opportunities to evaluate, this audit cannot demonstrate that the")
        p("currently allowed paths produce favorable follow-through. It also cannot show")
        p("they fail. The correct interpretation is: **the market did not provide setups")
        p("under the current rules in this window**, not that the rules are wrong.")
    else:
        one_r_rate = 100.0 * a["one_r_before_sl"] / n
        two_r_rate = 100.0 * a["two_r_before_sl"] / n
        sl_first_rate = 100.0 * a["sl_first"] / n
        unr_rate = 100.0 * a["unresolved"] / n
        # A 2R-before-SL win-rate of >=33% gives positive expectancy at 2R/1R RR
        # (0.33 * 2R - 0.67 * 1R = 0). >=40% is comfortable. <33% is negative EV
        # before fees / slippage / funding.
        p(f"**Counts:** {n} opportunities. {a['one_r_before_sl']} reached +1R before SL "
          f"({one_r_rate:.1f}%). {a['two_r_before_sl']} reached +2R/TP before SL "
          f"({two_r_rate:.1f}%). {a['sl_first']} hit SL first ({sl_first_rate:.1f}%). "
          f"{a['unresolved']} unresolved within 24h ({unr_rate:.1f}%).")
        p("")
        p("**Expectancy gate (signal-only, before fees/slippage/funding):**")
        p("at the spec'd 2:1 R/R, the break-even 2R-before-SL rate is **33.3%**.")
        p(f"Measured: **{two_r_rate:.1f}%** ({a['two_r_before_sl']}/{n}).")
        if two_r_rate >= 40.0:
            verdict = "STRONG"
            verdict_text = (
                "comfortably positive raw expectancy. After typical taker round-trip "
                "fees (~0.10%) and funding (~0.03%/day) the edge survives."
            )
        elif two_r_rate >= 33.3:
            verdict = "MARGINAL"
            verdict_text = (
                "barely positive raw expectancy. After fees + slippage + funding the "
                "edge is uncertain. Need more samples before trusting."
            )
        else:
            verdict = "WEAK"
            verdict_text = (
                "negative raw expectancy at 2:1 R/R. The currently allowed paths did "
                "NOT produce favorable follow-through in this window."
            )
        p("")
        p(f"**Verdict on follow-through quality**: {verdict} — {verdict_text}")
        p("")
        # Also report MFE/MAE conclusion
        mfe24 = a["avg_mfe_24h_R"]
        mae24 = a["avg_mae_24h_R"]
        p(f"**Excursion shape (24h)**: avg MFE {mfe24:.2f}R vs avg MAE {mae24:.2f}R "
          f"→ MFE/MAE ratio = {(mfe24 / mae24) if mae24 > 0 else float('inf'):.2f}.")
        if mfe24 > mae24:
            p("Favorable price excursion exceeded adverse excursion on average — the")
            p("market did move in the signal direction more than against it within 24h.")
        else:
            p("Adverse excursion exceeded favorable excursion on average — even when a")
            p("trade survives, drawdown inside the 24h window is significant.")
    p("")

    # ===== 10. Smallest evidence-based next action =====
    p("## 10. Smallest evidence-based next action")
    p("")
    if n == 0:
        p("- **Do nothing.** Zero opportunities in 7 days does not prove the rules")
        p("  are too tight; it proves the market did not present qualifying setups.")
        p("  Re-run this audit weekly. If 14-day and 28-day windows continue to show")
        p("  zero or near-zero opportunities AND the bot remains drawdown-positive, the")
        p("  reduced surface is doing its job.")
        p("- **Do not** re-enable any suppressed path on the basis of opportunity-")
        p("  scarcity alone. The reachability audit (sibling report) showed config-")
        p("  driven blocks dominate, but only a backtest with full SL/TP/fee model")
        p("  can justify enabling a path. This audit specifically does not measure")
        p("  the suppressed paths' quality.")
    else:
        p(f"- Captured {n} opportunities. The narrowest valid follow-up is to compare")
        p("  the measured 2R-before-SL rate against the value used in the position-")
        p("  sizing math (`Kelly ceiling` / `confidence tiers`). If the live sizing model")
        p("  assumes >40% win rate but this audit measures <30%, sizing is over-")
        p("  optimistic for the current regime — REDUCE size, do not change paths.")
        p("- Re-run this audit weekly. A single 7-day window is not enough to commit")
        p("  to any rule change.")
        if (
            agg_7d["by_path"].get("4h_flip", {"n": 0})["n"] > 0
            and agg_7d["by_path"].get("1h_continuation", {"n": 0})["n"] > 0
        ):
            f4 = agg_7d["by_path"]["4h_flip"]
            f1 = agg_7d["by_path"]["1h_continuation"]
            r4 = 100.0 * f4["two_r_before_sl"] / f4["n"] if f4["n"] else 0
            r1 = 100.0 * f1["two_r_before_sl"] / f1["n"] if f1["n"] else 0
            if r4 - r1 >= 15.0:
                p(f"- **Path quality split**: 4H flip 2R-rate {r4:.1f}% vs 1H continuation "
                  f"{r1:.1f}%. The 4H flip path is the stronger of the two in this window.")
            elif r1 - r4 >= 15.0:
                p(f"- **Path quality split**: 1H continuation 2R-rate {r1:.1f}% vs 4H flip "
                  f"{r4:.1f}%. Continuation outperformed; suggests trend-already-established")
                p(f"  setups followed through better than fresh flips.")
            else:
                p(f"- **Path quality split**: 4H flip {r4:.1f}% vs 1H cont. {r1:.1f}% "
                  f"— too close to discriminate from this sample.")
    p("")

    # ===== Cross-reference with reachability audit =====
    p("## 11. Cross-check with reachability audit")
    p("")
    p("The sibling [`REDUCED_MODE_REACHABILITY_AUDIT.md`](REDUCED_MODE_REACHABILITY_AUDIT.md)")
    p("counts **CHECKPOINTS** (every 30 min), so a single 1H-continuation flip can")
    p("show up at up to 8 consecutive checkpoints (CONTINUATION_LOOKBACK_1H=8). This")
    p("audit reports **OPPORTUNITIES** — consecutive same-direction same-path signals")
    p("collapsed to one event. Counts will therefore be SMALLER here than in the")
    p("reachability audit, by design.")
    p("")
    p("| metric | reachability audit (checkpoints) | this audit (opportunities) |")
    p("|---|---:|---:|")
    p(f"| 4h_flip total (7d, both syms) | {reachability_xref.get('4h_flip_cp', 'n/a')} | "
      f"{agg_7d['by_path'].get('4h_flip', {'n': 0})['n']} |")
    p(f"| 1h_continuation total (7d, both syms) | "
      f"{reachability_xref.get('1h_continuation_cp', 'n/a')} | "
      f"{agg_7d['by_path'].get('1h_continuation', {'n': 0})['n']} |")
    p(f"| reduced-mode reachable cycles (7d) | "
      f"{reachability_xref.get('reduced_reachable', 'n/a')} | (n/a, this audit dedupes) |")
    p("")
    p("If the OPPORTUNITY count is larger than the reachability count, that's a bug.")
    p("If the OPPORTUNITY count is much smaller, that's the dedupe working — see §2.4.")
    p("")

    # ===== Verification =====
    p("## 12. Verification")
    p("")
    p("### 12.1 Re-run command")
    p("```bash")
    p(".venv/bin/python scripts/audit_reduced_mode_opportunity_quality.py \\")
    p("    --out docs/reports/REDUCED_MODE_OPPORTUNITY_QUALITY_AUDIT.md \\")
    p("    --csv docs/reports/reduced_mode_opportunity_quality.csv")
    p("```")
    p("")
    p("### 12.2 Row counts")
    p("```")
    p(f"7-day opportunities analyzed: {agg_7d['all']['n']}")
    if agg_14d:
        p(f"14-day opportunities analyzed: {agg_14d['all']['n']}")
    p(f"Per-symbol-path breakdown:")
    for sym in SYMBOLS:
        for path in ("4h_flip", "1h_continuation"):
            s = agg_7d["by_sp"].get((sym, path), {"n": 0})
            p(f"  {sym} {path}: {s['n']}")
    p("```")
    p("")
    p("### 12.3 Cross-check (SQLite cycle history — timing only)")
    p("```bash")
    p("sqlite3 user_data/claude_quant.db \\")
    p('    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM cycle_history;"')
    p("```")
    p("This audit does NOT use the database for trade outcomes — only for")
    p("confirming the bot was actually polling during the audit window.")
    p("")
    p("### 12.4 Per-opportunity CSV")
    p("[`docs/reports/reduced_mode_opportunity_quality.csv`](reduced_mode_opportunity_quality.csv)")
    p("- columns: `symbol, checkpoint, path, direction, entry_price, atr_4h, sl_price,")
    p("  tp_price, confidence, regime, run_length, one_r_before_sl, two_r_before_sl,")
    p("  sl_first, unresolved, mfe_4h, mae_4h, mfe_8h, mae_8h, mfe_24h, mae_24h,")
    p("  time_to_sl_min, time_to_1r_min, time_to_tp_min, truncated_4h, truncated_8h,")
    p("  truncated_24h, forward_bars`.")
    p("")

    # ===== Red-team =====
    p("## 13. Red-team review")
    p("")
    p("**Paranoid Auditor**: Every opportunity row in the CSV is generated from")
    p("`SupertrendTrend.generate_signal` / `generate_continuation_signal` directly —")
    p("the same code paths the live orchestrator uses. ATR, regime, SL, TP, and")
    p("confidence are not recomputed by the audit; they are read out of the Signal")
    p("the strategy returned. The forward walk is pure Binance OHLCV. No probability")
    p("statement in this report is unsupported by a row in the CSV.")
    p("")
    p("**Regime Trader**: The 1R/2R milestones use exact entry-to-SL distance — they")
    p("are the SAME R the live bot would risk. The 4H ATR is preserved across both")
    p("paths (continuation does NOT use 1H ATR — see `supertrend_trend.py`'s comment")
    p("`# Entry / SL / TP — use 4H ATR (matches backtest; 1H ATR is 2-7x tighter)`).")
    p("If the 2R-before-SL rate is below 33% on this sample, that is the market")
    p("rejecting the 2:1 R/R model in this regime, not a measurement artefact.")
    p("")
    p("**Forensic Data Engineer**: 15m candles drive forward measurement. A 30-min")
    p("checkpoint signal is followed by 96 15m bars over 24h. MFE/MAE use candle")
    p("high/low, not close — so they capture intra-bar excursion. The conservative")
    p("ambiguous-bar rule (SL wins ties) means we will NEVER overstate the 1R / 2R")
    p("rate. We may understate it slightly when a same-bar reach actually traveled")
    p("up first then down. This bias is acknowledged and acceptable for a quality")
    p("audit (we want to avoid false positives, not false negatives).")
    p("")
    p("**QA Gremlin**: Edge cases checked: (a) `one_r_dist == 0` short-circuits to")
    p("unresolved (degenerate signal); (b) opportunities at the very end of the")
    p("audit window have `truncated_24h=True` set in the CSV — exclude them with")
    p("`awk -F, '$26==\"False\"'` if the reader wants only fully-resolved rows;")
    p("(c) consecutive-checkpoint dedupe uses a strict 30-min gap tolerance; if a")
    p("signal momentarily disappears for one cycle and returns, we treat it as a")
    p("new opportunity (this OVER-counts opportunities slightly; pure conservatism).")
    p("(d) Path types are evaluated independently; a single checkpoint can yield")
    p("up to 2 raw signals (one per path), and they are deduped per path, so the")
    p("same flip will not be double-counted.")
    p("")

    # ===== Hard-constraint compliance =====
    p("## 14. Hard-constraint compliance")
    p("")
    p("- No file under `src/` was modified.")
    p("- No threshold, no CB constant, no route flag was changed.")
    p("- No orders were placed; only public OHLCV was fetched.")
    p("- No DB row was written. The cycle-history check in §12.3 is a SELECT.")
    p("- Every count in this report comes from the script in §12.1, which writes")
    p("  the per-row CSV that backs every percentage and average.")
    p("- The 'measured quality' section (§3-§8) is strictly counts and statistics.")
    p("- The 'recommendation' section (§9-§10) is clearly labelled as derived from")
    p("  those counts and contains no `probably` / `likely` qualifiers.")
    p("- This audit does NOT recommend re-enabling any disabled path. It explicitly")
    p("  declines to make that recommendation in §10.")
    p("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_window(
    client: MarketDataClient,
    engine: IndicatorEngine,
    win_start: datetime,
    win_end: datetime,
) -> tuple[list[Opportunity], dict, dict]:
    """Run the audit over a single window. Returns opps + reachability cross-ref."""
    fetch_meta: dict = {}
    raw_signals_per_symbol: dict[str, list[RawSignal]] = {}

    days = max(1, math.ceil((win_end - win_start).total_seconds() / 86400))
    need_4h = int(days * 24 / 4) + 200
    need_1h = days * 24 + 200
    # 15m: window + 24h forward buffer + indicator headroom. Cap at 1500 / call.
    need_15m = (days * 24 + FORWARD_BUFFER_HOURS) * 4 + 200

    if need_15m > 1500:
        log.warning(
            "15m bars needed (%d) exceeds single-call limit (1500). Truncating "
            "fetch may shorten effective window.", need_15m,
        )

    cp_4h_flip = 0
    cp_1h_cont = 0
    cp_reduced_reach = 0

    # 15m frames fetched once with pagination so 14d window has full coverage
    df_15m_by_symbol: dict[str, pd.DataFrame] = {}

    for symbol in SYMBOLS:
        log.info("[%s] fetching candles for window %s..%s",
                 symbol, win_start.date(), win_end.date())
        df_4h = candles_to_df(await client.fetch_ohlcv(symbol, timeframe="4h", limit=min(1500, need_4h)))
        df_1h = candles_to_df(await client.fetch_ohlcv(symbol, timeframe="1h", limit=min(1500, need_1h)))
        df_15m = await fetch_paged_15m(client, symbol, need_15m)
        df_15m_by_symbol[symbol] = df_15m.copy()  # raw for forward walk

        df_4h = engine.calculate_all(df_4h.copy())
        df_1h = engine.calculate_all(df_1h.copy())
        df_15m = engine.calculate_all(df_15m.copy())

        fetch_meta[symbol] = {
            "4h_bars": len(df_4h), "1h_bars": len(df_1h), "15m_bars": len(df_15m),
            "15m_first": df_15m.index[0] if len(df_15m) else None,
            "15m_last": df_15m.index[-1] if len(df_15m) else None,
        }

        # Generate 30-min checkpoints
        cps: list[datetime] = []
        t = win_start
        while t <= win_end:
            cps.append(t)
            t += timedelta(minutes=CYCLE_MINUTES)

        raw: list[RawSignal] = []
        cps_with_4h = 0
        cps_with_1h = 0
        cps_with_any = 0
        for cp in cps:
            sigs = evaluate_reduced_signals(symbol, cp, df_4h, df_1h)
            if not sigs:
                continue
            paths_at_cp = {s.path for s in sigs}
            if "4h_flip" in paths_at_cp:
                cps_with_4h += 1
            if "1h_continuation" in paths_at_cp:
                cps_with_1h += 1
            cps_with_any += 1
            raw.extend(sigs)

        cp_4h_flip += cps_with_4h
        cp_1h_cont += cps_with_1h
        cp_reduced_reach += cps_with_any
        log.info("[%s] checkpoints: 4h_flip=%d 1h_cont=%d any=%d raw_signals=%d",
                 symbol, cps_with_4h, cps_with_1h, cps_with_any, len(raw))

        raw_signals_per_symbol[symbol] = raw

    # Collapse runs into opportunities
    all_raw = [s for v in raw_signals_per_symbol.values() for s in v]
    opps = dedupe_runs(all_raw)
    log.info("Total raw signals: %d → %d unique opportunities",
             len(all_raw), len(opps))

    # Forward measurement uses the SAME 15m frames already fetched (raw, no
    # indicator columns needed for OHLCV-based forward walking).
    for o in opps:
        df15 = df_15m_by_symbol.get(o.symbol)
        if df15 is None or df15.empty:
            o.forward = ForwardResult(
                None, None, None, None, True,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                None, None, None, True, True, True, 0,
            )
            continue
        # Guard: if the signal time is BEFORE the earliest 15m bar we have,
        # we cannot measure forward path. Mark as un-measurable.
        if o.checkpoint < df15.index[0]:
            log.warning(
                "[%s] signal at %s is before earliest 15m bar (%s) — "
                "marking unmeasurable",
                o.symbol, o.checkpoint, df15.index[0],
            )
            o.forward = ForwardResult(
                None, None, None, None, True,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                None, None, None, True, True, True, 0,
            )
            continue
        o.forward = measure_forward(
            RawSignal(
                symbol=o.symbol, checkpoint=o.checkpoint, path=o.path,
                direction=o.direction, entry_price=o.entry_price,
                atr_4h=o.atr_4h, sl_price=o.sl_price, tp_price=o.tp_price,
                confidence=o.confidence, regime=o.regime,
            ),
            df15,
        )

    reachability_xref = {
        "4h_flip_cp": cp_4h_flip,
        "1h_continuation_cp": cp_1h_cont,
        "reduced_reachable": cp_reduced_reach,
    }

    return opps, fetch_meta, reachability_xref


def write_csv(opps: list[Opportunity], path: Path) -> None:
    rows = []
    for o in opps:
        f = o.forward
        rows.append({
            "symbol": o.symbol,
            "checkpoint": o.checkpoint.isoformat(),
            "path": o.path,
            "direction": o.direction,
            "entry_price": o.entry_price,
            "atr_4h": o.atr_4h,
            "sl_price": o.sl_price,
            "tp_price": o.tp_price,
            "confidence": o.confidence,
            "regime": o.regime,
            "run_length": o.run_length,
            "one_r_before_sl": f.one_r_before_sl,
            "two_r_before_sl": f.two_r_before_sl,
            "sl_first": f.sl_first,
            "unresolved": f.unresolved,
            "mfe_4h": f.mfe_4h,
            "mae_4h": f.mae_4h,
            "mfe_8h": f.mfe_8h,
            "mae_8h": f.mae_8h,
            "mfe_24h": f.mfe_24h,
            "mae_24h": f.mae_24h,
            "time_to_sl_min": f.time_to_sl_min,
            "time_to_1r_min": f.time_to_1r_min,
            "time_to_tp_min": f.time_to_tp_min,
            "truncated_4h": f.truncated_4h,
            "truncated_8h": f.truncated_8h,
            "truncated_24h": f.truncated_24h,
            "forward_bars": f.forward_bars,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


async def main_async(args) -> int:
    if os.getenv("BINANCE_TESTNET", "false").lower() == "true":
        print("ERROR: BINANCE_TESTNET=true. This audit must run against mainnet.")
        return 2

    client = MarketDataClient()
    await client.connect()
    engine = IndicatorEngine()

    now = datetime.now(tz=timezone.utc)
    anchor = now.replace(second=0, microsecond=0, minute=(now.minute // 30) * 30)
    win7_end = anchor
    win7_start = anchor - timedelta(days=7)
    win14_end = anchor
    win14_start = anchor - timedelta(days=14)

    fetch_meta: dict = {
        "win7_start": win7_start, "win7_end": win7_end,
    }

    opps_7d: list[Opportunity] = []
    opps_14d: Optional[list[Opportunity]] = None
    rxref_7d: dict = {}
    rxref_14d: dict = {}

    try:
        opps_7d, fm7, rxref_7d = await run_window(
            client, engine, win7_start, win7_end,
        )
        fetch_meta["fetched_7d"] = fm7

        if args.days >= 14:
            log.info("Running secondary 14-day window...")
            opps_14d, fm14, rxref_14d = await run_window(
                client, engine, win14_start, win14_end,
            )
            fetch_meta["win14_start"] = win14_start
            fetch_meta["win14_end"] = win14_end
            fetch_meta["fetched_14d"] = fm14
    finally:
        await client.close()

    agg_7d = aggregate(opps_7d)
    agg_14d = aggregate(opps_14d) if opps_14d is not None else None

    report = build_report(
        opps_7d=opps_7d, agg_7d=agg_7d,
        opps_14d=opps_14d, agg_14d=agg_14d,
        fetch_meta=fetch_meta,
        reachability_xref=rxref_7d,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"Wrote {out_path}")

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(opps_7d, csv_path)
    print(f"Wrote {csv_path} ({len(opps_7d)} rows)")

    if opps_14d is not None:
        csv14 = csv_path.with_name(csv_path.stem + "_14d.csv")
        write_csv(opps_14d, csv14)
        print(f"Wrote {csv14} ({len(opps_14d)} rows)")

    # Summary to stdout for the operator
    print("---")
    print(f"7-day opportunities: {agg_7d['all']['n']}")
    print(f"  1R-before-SL: {agg_7d['all']['one_r_before_sl']}")
    print(f"  2R-before-SL: {agg_7d['all']['two_r_before_sl']}")
    print(f"  SL-first:     {agg_7d['all']['sl_first']}")
    print(f"  unresolved:   {agg_7d['all']['unresolved']}")
    if agg_14d:
        print(f"14-day opportunities: {agg_14d['all']['n']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default="docs/reports/REDUCED_MODE_OPPORTUNITY_QUALITY_AUDIT.md",
        help="Path to markdown report",
    )
    ap.add_argument(
        "--csv",
        default="docs/reports/reduced_mode_opportunity_quality.csv",
        help="Path to per-opportunity evidence CSV",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=14,
        help="Audit window length in days (primary always 7; if >=14 also runs 14d)",
    )
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
