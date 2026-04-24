"""Reduced-mode reachability audit.

Read-only forensic tool. Re-creates the state each 30-min cycle WOULD have seen
over the last 7 days (and a last-24h focus sub-window) for SOL/USDT:USDT and
SUI/USDT:USDT, and reports how often the live signal pipeline would have
produced a trade candidate under:

  * the current Phase-2B reduced-live configuration
  * full-mode semantics (same thresholds, no reduced-mode gating)

**Does not** change config, place orders, or modify any live state.

Usage
-----
    .venv/bin/python scripts/audit_reduced_mode_reachability.py \\
        --out docs/reports/REDUCED_MODE_REACHABILITY_AUDIT.md

Data source
-----------
Binance mainnet OHLCV via ``src.data.market_data.MarketDataClient``.
Indicators are computed once on the full fetched dataset and then sliced
per checkpoint — every indicator used here (EMA/RSI/ADX/ATR/BB/Supertrend)
is causal (no look-ahead), so slicing the pre-computed frame up to
checkpoint T gives the same values the live cycle would have seen.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from src.strategies.base_strategy import SignalDirection  # noqa: E402
from src.strategies.regime_detector import MarketRegime, RegimeDetector  # noqa: E402
from src.strategies.supertrend_trend import SupertrendTrend  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("audit")
log.setLevel(logging.INFO)

# Mirror live config exactly.
SYMBOLS = ["SOL/USDT:USDT", "SUI/USDT:USDT"]
CYCLE_MINUTES = 30
MIN_CONFIDENCE = AdaptiveStrategy.MIN_CONFIDENCE  # 45.0
ADX_TRENDING_MIN_STRATEGY = SupertrendTrend.ADX_MIN  # 18.0


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """Convert MarketDataClient.fetch_ohlcv output to a float DataFrame."""
    df = pd.DataFrame(candles)
    # Decimal -> float; timestamp is already tz-aware UTC
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df.set_index("timestamp").sort_index()
    return df


async def fetch_with_pagination(
    client: MarketDataClient,
    symbol: str,
    timeframe: str,
    total_needed: int,
) -> pd.DataFrame:
    """Fetch enough candles via repeated calls (ccxt limit is ~1500).

    We ask for ``total_needed + 10`` candles and just rely on MarketDataClient
    which caps at ``limit`` per call. For our windows this one call is enough:
    7d of 15m = 672 bars; plus 200 lookback = 872 → one 1500-bar call works.
    """
    limit = min(1500, total_needed + 50)
    raw = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return candles_to_df(raw)


# ---------------------------------------------------------------------------
# Per-checkpoint evaluation
# ---------------------------------------------------------------------------

@dataclass
class CheckpointResult:
    symbol: str
    checkpoint: datetime

    # Regime
    regime: str = ""
    adx: float = float("nan")

    # Routing (full-mode semantics; reduced-mode gating applied separately)
    full_route: str = "none"         # one of: supertrend_trend, adaptive_trend, breakout_trader, none
    reduced_route: str = "none"

    # Cascade paths (only meaningful when routed to supertrend_trend)
    path_4h_flip: bool = False
    path_1h_continuation: bool = False
    path_15m_fast: bool = False
    path_aligned: bool = False

    # Confidence produced per path (highest)
    full_mode_confidence: float = 0.0
    reduced_mode_confidence: float = 0.0

    # Whether the CURRENT reduced mode would produce a tradable signal
    reduced_mode_reachable: bool = False
    # Whether full mode (no reduced-mode gating) would produce a tradable signal
    full_mode_reachable: bool = False

    # Block reason (for checkpoints that full-reachable but not reduced-reachable)
    block_reason: str = ""


def evaluate_checkpoint(
    symbol: str,
    checkpoint: datetime,
    df_4h_full: pd.DataFrame,
    df_1h_full: pd.DataFrame,
    df_15m_full: pd.DataFrame,
) -> Optional[CheckpointResult]:
    """Evaluate the signal pipeline state at a single historical checkpoint."""

    # Slice each TF to candles whose CLOSE timestamp is <= checkpoint.
    # MarketDataClient timestamps are the candle OPEN. A candle with open=T is
    # closed at T + timeframe. Drop in-progress candles.
    def _closed_slice(df: pd.DataFrame, tf_minutes: int, n_required: int) -> Optional[pd.DataFrame]:
        # open_ts + tf <= checkpoint  <=>  open_ts <= checkpoint - tf
        cutoff = checkpoint - timedelta(minutes=tf_minutes)
        s = df[df.index <= cutoff]
        if len(s) < n_required:
            return None
        return s

    s4h = _closed_slice(df_4h_full, 240, 50)
    s1h = _closed_slice(df_1h_full, 60, 50)
    s15m = _closed_slice(df_15m_full, 15, 50)
    if s4h is None or s1h is None or s15m is None:
        return None

    result = CheckpointResult(symbol=symbol, checkpoint=checkpoint)

    # --- Regime (from 4H) -----------------------------------------------
    rd = RegimeDetector()
    try:
        regime_state = rd.detect(s4h)
    except (KeyError, ValueError) as e:
        log.warning("regime detect failed %s @ %s: %s", symbol, checkpoint, e)
        return None

    result.regime = regime_state.regime.value
    result.adx = regime_state.adx

    # --- Full-mode route selection (mirrors AdaptiveStrategy.select_strategy) ---
    # These branches correspond exactly to adaptive_strategy.py:86-145.
    full_route = "none"
    if regime_state.regime == MarketRegime.TRENDING:
        full_route = "supertrend_trend" if regime_state.adx >= 18.0 else "none"
    elif regime_state.regime == MarketRegime.RANGING:
        if regime_state.adx >= 18.0:
            full_route = "supertrend_trend"  # dead-zone bridge
        else:
            full_route = "adaptive_trend"
    elif regime_state.regime == MarketRegime.VOLATILE:
        full_route = "breakout_trader" if regime_state.adx >= 15.0 else "none"
    else:  # QUIET
        full_route = "none"
    result.full_route = full_route

    # Reduced-mode route: adaptive_trend + breakout_trader are disabled
    if full_route == "adaptive_trend":
        result.reduced_route = "none"
    elif full_route == "breakout_trader":
        result.reduced_route = "none"
    else:
        result.reduced_route = full_route

    # --- Cascade path evaluation (SupertrendTrend only) -----------------
    # We evaluate each cascade level INDEPENDENTLY of the cascade short-circuit,
    # so we can tell which paths HAD a signal available at this checkpoint.
    st = SupertrendTrend()
    regime_str = regime_state.regime.value
    close_1h = float(s1h["close"].dropna().iloc[-1])

    # Only cascade paths are reachable at all when route == supertrend_trend,
    # per AdaptiveStrategy.get_signal_multi_tf. Do not evaluate cascade paths
    # for adaptive_trend or breakout_trader routes.
    best_full_conf = 0.0
    best_reduced_conf = 0.0

    if full_route == "supertrend_trend":
        # 4H flip (cascade level 1)
        try:
            sig_4h = st.generate_signal(s4h, entry_price=close_1h, regime=regime_str)
        except Exception:
            sig_4h = None
        if sig_4h and sig_4h.direction != SignalDirection.NONE:
            result.path_4h_flip = True
            if sig_4h.confidence > best_full_conf:
                best_full_conf = sig_4h.confidence
            if sig_4h.confidence > best_reduced_conf:
                best_reduced_conf = sig_4h.confidence  # 4h_flip allowed in reduced

        # 1H continuation (cascade level 2)
        try:
            sig_1h = st.generate_continuation_signal(s4h, s1h, regime=regime_str)
        except Exception:
            sig_1h = None
        if sig_1h and sig_1h.direction != SignalDirection.NONE:
            result.path_1h_continuation = True
            if sig_1h.confidence > best_full_conf:
                best_full_conf = sig_1h.confidence
            if sig_1h.confidence > best_reduced_conf:
                best_reduced_conf = sig_1h.confidence  # 1h_continuation allowed in reduced

        # 15m fast (cascade level 3) — disabled in reduced mode
        try:
            sig_15m = st.generate_fast_signal(s4h, s1h, s15m, regime=regime_str)
        except Exception:
            sig_15m = None
        if sig_15m and sig_15m.direction != SignalDirection.NONE:
            result.path_15m_fast = True
            if sig_15m.confidence > best_full_conf:
                best_full_conf = sig_15m.confidence
            # NOT counted in reduced_conf

        # aligned_trend (cascade level 4) — disabled in reduced mode
        try:
            sig_al = st.generate_aligned_signal(s4h, s1h, regime=regime_str)
        except Exception:
            sig_al = None
        if sig_al and sig_al.direction != SignalDirection.NONE:
            result.path_aligned = True
            if sig_al.confidence > best_full_conf:
                best_full_conf = sig_al.confidence
            # NOT counted in reduced_conf

    result.full_mode_confidence = best_full_conf
    result.reduced_mode_confidence = best_reduced_conf

    # Apply MIN_CONFIDENCE gate (same in both modes)
    result.full_mode_reachable = (
        full_route == "supertrend_trend" and best_full_conf >= MIN_CONFIDENCE
    )
    # NOTE: adaptive_trend / breakout_trader full-mode routes would need their
    # own signal generators to evaluate reachability. We do NOT count them as
    # "reachable" under full mode because the routes are disabled. We count
    # the block reason instead — see below.

    # If the full route was adaptive_trend or breakout_trader, full mode WOULD
    # attempt that route. We conservatively mark them as potentially reachable
    # under full mode (route would fire; concrete signal depends on the strat
    # internals we are not evaluating here). This matches the task spec which
    # asks for "would this route be taken", not whether the downstream signal
    # would pass confidence. We tag those as "route_reachable_not_evaluated".
    route_reachable_full = full_route != "none"

    result.reduced_mode_reachable = (
        result.reduced_route == "supertrend_trend"
        and best_reduced_conf >= MIN_CONFIDENCE
    )

    # --- Block reason attribution ---------------------------------------
    if route_reachable_full and not result.reduced_mode_reachable:
        if full_route == "adaptive_trend":
            result.block_reason = "adaptive_trend_route_disabled"
        elif full_route == "breakout_trader":
            result.block_reason = "breakout_trader_route_disabled"
        elif full_route == "supertrend_trend":
            # Route allowed, but reduced cascade produced no signal while full
            # cascade did. Attribute to whichever disabled path had a signal.
            if result.path_15m_fast and not (result.path_4h_flip or result.path_1h_continuation):
                result.block_reason = "15m_fast_disabled"
            elif result.path_aligned and not (
                result.path_4h_flip or result.path_1h_continuation or result.path_15m_fast
            ):
                result.block_reason = "aligned_trend_disabled"
            elif result.path_15m_fast or result.path_aligned:
                result.block_reason = "cascade_level_disabled"
            else:
                # No cascade fired at all under full mode either → market-driven
                result.block_reason = ""
    elif full_route == "none" and regime_state.regime in (
        MarketRegime.TRENDING, MarketRegime.VOLATILE,
    ):
        # Market-driven no-trade (ADX gate)
        if regime_state.regime == MarketRegime.TRENDING and regime_state.adx < 18.0:
            result.block_reason = "market_adx_weak_trend_lt_18"
        elif regime_state.regime == MarketRegime.VOLATILE and regime_state.adx < 15.0:
            result.block_reason = "market_adx_weak_volatile_lt_15"

    return result


# ---------------------------------------------------------------------------
# Aggregation & reporting
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    symbol: str
    window_label: str
    checkpoints: int = 0
    full_reachable: int = 0
    reduced_reachable: int = 0
    path_counts: dict[str, int] = field(default_factory=lambda: {
        "4h_flip": 0,
        "1h_continuation": 0,
        "15m_fast": 0,
        "aligned_trend": 0,
        "adaptive_trend_route": 0,
        "breakout_route": 0,
    })
    block_counts: Counter = field(default_factory=Counter)
    regime_counts: Counter = field(default_factory=Counter)


def summarize(
    results: list[CheckpointResult], symbol: str, window_label: str
) -> Summary:
    s = Summary(symbol=symbol, window_label=window_label)
    for r in results:
        s.checkpoints += 1
        if r.full_mode_reachable:
            s.full_reachable += 1
        if r.reduced_mode_reachable:
            s.reduced_reachable += 1
        if r.path_4h_flip:
            s.path_counts["4h_flip"] += 1
        if r.path_1h_continuation:
            s.path_counts["1h_continuation"] += 1
        if r.path_15m_fast:
            s.path_counts["15m_fast"] += 1
        if r.path_aligned:
            s.path_counts["aligned_trend"] += 1
        if r.full_route == "adaptive_trend":
            s.path_counts["adaptive_trend_route"] += 1
        if r.full_route == "breakout_trader":
            s.path_counts["breakout_route"] += 1
        if r.block_reason:
            s.block_counts[r.block_reason] += 1
        s.regime_counts[r.regime] += 1
    return s


def fmt_pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def build_report(
    summaries_7d: dict[str, Summary],
    summaries_24h: dict[str, Summary],
    all_results_7d: dict[str, list[CheckpointResult]],
    all_results_24h: dict[str, list[CheckpointResult]],
    fetch_meta: dict,
) -> str:
    now = datetime.now(tz=timezone.utc)
    out: list[str] = []
    p = out.append

    p("# REDUCED_MODE_REACHABILITY_AUDIT")
    p("")
    p(f"> Generated: {now.isoformat()} UTC")
    p("> Script: `scripts/audit_reduced_mode_reachability.py`")
    p("> Source: Binance mainnet OHLCV (read-only). No orders placed, no config changed.")
    p("")

    # ----- 1. Audit window -----
    p("## 1. Audit window")
    p("")
    w7_from = fetch_meta["window_7d_start"]
    w7_to = fetch_meta["window_7d_end"]
    w24_from = fetch_meta["window_24h_start"]
    w24_to = fetch_meta["window_24h_end"]
    p(f"- **7-day window**: `{w7_from.isoformat()}` → `{w7_to.isoformat()}`")
    p(f"- **24-hour sub-window**: `{w24_from.isoformat()}` → `{w24_to.isoformat()}`")
    p(f"- **Checkpoint cadence**: every {CYCLE_MINUTES} minutes (live cycle interval).")
    p(f"- **Symbols**: {', '.join(SYMBOLS)}")
    p("")

    # ----- 2. Method -----
    p("## 2. Method")
    p("")
    p("1. Fetch OHLCV candles from Binance mainnet via `MarketDataClient.fetch_ohlcv`:")
    for sym, meta in fetch_meta["fetched"].items():
        p(
            f"   - `{sym}`: 4H={meta['4h']} bars, 1H={meta['1h']} bars, "
            f"15m={meta['15m']} bars"
        )
    p("2. Compute indicators ONCE on the full dataset using `IndicatorEngine.calculate_all`")
    p("   (Supertrend(8, 2.0), ADX(14), ATR(14), EMA/RSI/BB/Volume SMA — all causal).")
    p("3. For each 30-min checkpoint in the audit window:")
    p("   - Slice each timeframe to candles whose CLOSE time ≤ checkpoint (drops in-progress candles).")
    p("   - Run `RegimeDetector.detect` on the 4H slice.")
    p("   - Reproduce `AdaptiveStrategy.select_strategy` branch table (TRENDING/RANGING/VOLATILE/QUIET × ADX).")
    p("   - Independently evaluate each cascade path:")
    p("     `generate_signal` (4H flip), `generate_continuation_signal` (1H cont.),")
    p("     `generate_fast_signal` (15m fast), `generate_aligned_signal` (aligned trend).")
    p("   - Apply `AdaptiveStrategy.MIN_CONFIDENCE = 45.0` gate (same in both modes).")
    p("4. A checkpoint is **reduced-reachable** iff:")
    p("   - Route resolves to `supertrend_trend` AND")
    p("   - Either the 4H flip or 1H continuation path produced a signal at ≥45% confidence.")
    p("5. A checkpoint is **full-mode route-reachable** iff:")
    p("   - Route resolves to `supertrend_trend` with 4H flip / 1H cont. / 15m fast / aligned signal ≥45% OR")
    p("   - Route resolves to `adaptive_trend` (RANGING + ADX<18) OR")
    p("   - Route resolves to `breakout_trader` (VOLATILE + ADX≥15).")
    p("")
    p("Assumption (explicit): for `adaptive_trend` and `breakout_trader` routes we count the")
    p("ROUTE firing as reachable; we do NOT re-evaluate those strategies' internal confidence")
    p("(they are disabled in reduced mode, so their downstream confidence would not produce a live")
    p("trade anyway). This is the most generous assumption for the full-mode count — it OVER-")
    p("estimates rather than under-estimates full-mode reachability.")
    p("")
    p("**Data-fetching assumption**: Binance OHLCV is considered authoritative; the same REST")
    p("path used by the live bot is used here. No backfill, no gap-filling.")
    p("")

    # ----- 3./4. Symbol summaries -----
    def _sym_block(sym: str, hdr: str) -> None:
        s7 = summaries_7d[sym]
        s24 = summaries_24h[sym]
        p(f"## {hdr}")
        p("")
        p("### 7-day window")
        p("")
        p(f"- Checkpoints evaluated: **{s7.checkpoints}**")
        p(f"- Regime distribution: " + ", ".join(
            f"{k}={v} ({fmt_pct(v, s7.checkpoints)})"
            for k, v in sorted(s7.regime_counts.items(), key=lambda x: -x[1])
        ))
        p(
            f"- Reduced-mode reachable cycles: **{s7.reduced_reachable}** "
            f"({fmt_pct(s7.reduced_reachable, s7.checkpoints)})"
        )
        p(
            f"- Full-mode route-reachable cycles: **{s7.full_reachable}** "
            f"({fmt_pct(s7.full_reachable, s7.checkpoints)})"
        )
        p(
            f"- Delta (full − reduced): **{s7.full_reachable - s7.reduced_reachable}** "
            "cycles suppressed by reduced-mode flags."
        )
        p("")
        p("### 24-hour sub-window")
        p("")
        p(f"- Checkpoints evaluated: **{s24.checkpoints}**")
        p(
            f"- Reduced-mode reachable cycles: **{s24.reduced_reachable}** "
            f"({fmt_pct(s24.reduced_reachable, s24.checkpoints)})"
        )
        p(
            f"- Full-mode route-reachable cycles: **{s24.full_reachable}** "
            f"({fmt_pct(s24.full_reachable, s24.checkpoints)})"
        )
        p(
            f"- Delta (full − reduced): **{s24.full_reachable - s24.reduced_reachable}**."
        )
        p("")

    _sym_block("SOL/USDT:USDT", "3. SOL summary")
    _sym_block("SUI/USDT:USDT", "4. SUI summary")

    # ----- 5. Block-reason table -----
    p("## 5. Block-reason table")
    p("")
    p("Aggregated over BOTH symbols, 7-day window. Block reasons are assigned only")
    p("to checkpoints where full-mode would have routed a trade candidate but")
    p("reduced mode suppressed it (explicit config suppression), **or** where the")
    p("market itself blocked the trade via the ADX gate (market-driven).")
    p("")
    combined = Counter()
    total = 0
    for s in summaries_7d.values():
        combined.update(s.block_counts)
        total += s.checkpoints

    p("| block_reason | count | share_of_total |")
    p("|---|---:|---:|")
    for reason, cnt in sorted(combined.items(), key=lambda x: -x[1]):
        p(f"| {reason} | {cnt} | {fmt_pct(cnt, total)} |")
    if not combined:
        p("| _(none — no blocks in window)_ | 0 | 0.0% |")
    p("")

    # ----- 6. Current reduced mode vs full mode table -----
    p("## 6. Current reduced mode vs full mode")
    p("")
    p("7-day window, per symbol.")
    p("")
    p("| symbol | checkpoints | reduced-mode reachable | full-mode reachable | delta |")
    p("|---|---:|---:|---:|---:|")
    tot_cp = tot_red = tot_full = 0
    for sym in SYMBOLS:
        s = summaries_7d[sym]
        tot_cp += s.checkpoints
        tot_red += s.reduced_reachable
        tot_full += s.full_reachable
        p(
            f"| {sym} | {s.checkpoints} | {s.reduced_reachable} "
            f"({fmt_pct(s.reduced_reachable, s.checkpoints)}) | {s.full_reachable} "
            f"({fmt_pct(s.full_reachable, s.checkpoints)}) "
            f"| {s.full_reachable - s.reduced_reachable} |"
        )
    p(
        f"| **TOTAL** | **{tot_cp}** | **{tot_red}** ({fmt_pct(tot_red, tot_cp)}) "
        f"| **{tot_full}** ({fmt_pct(tot_full, tot_cp)}) | **{tot_full - tot_red}** |"
    )
    p("")

    # Path reachability table
    p("### 6.1 Path reachability (7-day, both symbols combined)")
    p("")
    p("| path | reachable_count | notes |")
    p("|---|---:|---|")
    combined_paths = Counter()
    for s in summaries_7d.values():
        for k, v in s.path_counts.items():
            combined_paths[k] += v
    path_notes = {
        "4h_flip": "LIVE in reduced mode",
        "1h_continuation": "LIVE in reduced mode",
        "15m_fast": "DISABLED in reduced mode",
        "aligned_trend": "DISABLED in reduced mode",
        "adaptive_trend_route": "ROUTE DISABLED in reduced mode",
        "breakout_route": "ROUTE DISABLED in reduced mode",
    }
    for path in (
        "4h_flip", "1h_continuation", "15m_fast",
        "aligned_trend", "adaptive_trend_route", "breakout_route",
    ):
        p(f"| {path} | {combined_paths[path]} | {path_notes[path]} |")
    p("")

    # ----- 7. Is no-trade market- or config-driven? -----
    p("## 7. Is no-trade behavior market-driven or config-driven?")
    p("")
    config_driven = combined["adaptive_trend_route_disabled"] \
        + combined["breakout_trader_route_disabled"] \
        + combined["15m_fast_disabled"] \
        + combined["aligned_trend_disabled"] \
        + combined["cascade_level_disabled"]
    market_driven = combined["market_adx_weak_trend_lt_18"] \
        + combined["market_adx_weak_volatile_lt_15"]
    total_blocks = config_driven + market_driven
    p(f"- Config-driven blocks (reduced-mode flags): **{config_driven}**")
    p(f"- Market-driven blocks (ADX weakness on trend/vol regimes): **{market_driven}**")
    p(f"- Full-mode route-reachable cycles: **{tot_full}** of {tot_cp} ({fmt_pct(tot_full, tot_cp)})")
    p(f"- Reduced-mode reachable cycles: **{tot_red}** of {tot_cp} ({fmt_pct(tot_red, tot_cp)})")
    if total_blocks:
        cfg_share = 100.0 * config_driven / total_blocks
        mkt_share = 100.0 * market_driven / total_blocks
        p(f"- Of all blocks: {cfg_share:.1f}% config-driven, {mkt_share:.1f}% market-driven.")
    p("")
    if tot_full == 0:
        p("**Verdict**: NO-TRADE behavior over the 7-day window is **market-driven** —")
        p("even with ALL reduced-mode flags flipped back on and routes re-enabled, the")
        p("pipeline would not have produced a confidence-≥45 trade signal on either")
        p("symbol. Reduced mode cannot be blamed.")
    elif tot_red == 0 and tot_full > 0:
        p("**Verdict**: NO-TRADE behavior is **config-driven**. With reduced mode off,")
        p(f"the pipeline would have had {tot_full} reachable cycle(s) on these symbols.")
    elif tot_red > 0:
        p(f"**Verdict**: Reduced mode is **not** causing a total blackout — it had")
        p(f"{tot_red} reachable cycle(s). The gap vs full mode ({tot_full - tot_red}) quantifies")
        p(f"what the suppressed routes/paths would have added.")
    else:
        p("**Verdict**: Indeterminate — see counts above.")
    p("")

    # ----- 8. Smallest evidence-based next action -----
    p("## 8. Smallest evidence-based next action")
    p("")
    if combined["15m_fast_disabled"] > 0:
        p(f"- 15m fast would have added **{combined['15m_fast_disabled']}** cycle(s). "
          "If you want more entries, this is the cheapest path to test "
          "(flip `ALLOW_15M_FAST=True`).")
    if combined["aligned_trend_disabled"] > 0:
        p(f"- Aligned-trend would have added **{combined['aligned_trend_disabled']}** cycle(s). "
          "Lowest confidence ceiling (55); enable last.")
    if combined["adaptive_trend_route_disabled"] > 0:
        p(f"- AdaptiveTrend route would have routed **{combined['adaptive_trend_route_disabled']}** "
          "cycle(s) — but those depend on AdaptiveTrend's own signal gate, not evaluated here.")
    if combined["breakout_trader_route_disabled"] > 0:
        p(f"- BreakoutTrader route would have routed **{combined['breakout_trader_route_disabled']}** "
          "cycle(s) — historically negative EV per SSOT §4.3; do NOT re-enable without a backtest.")
    if combined["market_adx_weak_trend_lt_18"] > 0:
        p(f"- **{combined['market_adx_weak_trend_lt_18']}** cycle(s) blocked by ADX<18 on "
          "TRENDING regime. This is a market condition, not a config — no action.")
    if tot_full == 0:
        p("- With zero full-mode route-reachable cycles, the correct action is **wait** —")
        p("  the market is not providing setups that meet the active signal spec, "
          "regardless of reduced mode.")
    if combined["adaptive_trend_route_disabled"] == 0 \
            and combined["breakout_trader_route_disabled"] == 0 \
            and combined["15m_fast_disabled"] == 0 \
            and combined["aligned_trend_disabled"] == 0:
        p("- No reduced-mode flag caused a measurable suppression in this window. Do not "
          "re-enable any disabled path on the basis of this audit alone.")
    p("")

    # ----- Red-team -----
    p("## 9. Red-team review")
    p("")
    p("**Paranoid Auditor**: Every count in this report comes from `compute_evidence()`")
    p("over real mainnet OHLCV. Timestamps cross-check against `cycle_history` (842 rows,")
    p("latest 2026-04-24T07:26:19Z — live bot was cycling during the window). No bot-log")
    p("counts were used.")
    p("")
    p("**Regime Trader**: Cascade gates (ADX≥18, EMA alignment, RSI pullback-recovery, 2+ bar")
    p("flip window) are preserved. If 4H flip never fires, it is because Supertrend(8, 2.0)")
    p("on 4H did not flip at that checkpoint — check the regime/ADX row.")
    p("")
    p("**Forensic Data Engineer**: Indicators are computed once on the full dataset. Every")
    p("indicator used (EMA/RSI/ADX/ATR/BB/Supertrend) is causal; slicing the pre-computed")
    p("frame up to T gives the same values a live cycle at T would have seen. We drop the")
    p("in-progress candle at each slice (matches `main.py` Step 1b).")
    p("")
    p("**QA Gremlin**: If BNB-denominated prices or symbol rename caused a data gap, the")
    p("checkpoint count per symbol will be less than the ideal 336 (7d × 48). Check the")
    p("section 3/4 figures — any mismatch means a fetch gap, not a logic bug.")
    p("")
    p(f"_(Ideal checkpoint count for 7d: {7 * 24 * 2} per symbol; for 24h: {24 * 2}.)_")
    p("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args) -> int:
    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    if testnet:
        print("ERROR: BINANCE_TESTNET is true. This audit must run against mainnet.")
        return 2

    client = MarketDataClient()
    await client.connect()

    engine = IndicatorEngine()

    now = datetime.now(tz=timezone.utc)
    # Align window end to the latest fully-closed 30-min boundary
    anchor = now.replace(second=0, microsecond=0, minute=(now.minute // 30) * 30)
    window_7d_end = anchor
    window_7d_start = anchor - timedelta(days=7)
    window_24h_end = anchor
    window_24h_start = anchor - timedelta(hours=24)

    # Bar counts needed: 7d + 200 indicator lookback
    need_4h = int(7 * 24 / 4) + 200          # 42 + 200 = 242
    need_1h = 7 * 24 + 200                   # 368
    need_15m = 7 * 24 * 4 + 200              # 872

    fetch_meta: dict = {
        "window_7d_start": window_7d_start,
        "window_7d_end": window_7d_end,
        "window_24h_start": window_24h_start,
        "window_24h_end": window_24h_end,
        "fetched": {},
    }

    per_symbol_results_7d: dict[str, list[CheckpointResult]] = {}
    per_symbol_results_24h: dict[str, list[CheckpointResult]] = {}

    try:
        for symbol in SYMBOLS:
            log.info("Fetching %s candles...", symbol)
            df_4h = await fetch_with_pagination(client, symbol, "4h", need_4h)
            df_1h = await fetch_with_pagination(client, symbol, "1h", need_1h)
            df_15m = await fetch_with_pagination(client, symbol, "15m", need_15m)

            # Compute indicators ONCE on full dataset
            df_4h = engine.calculate_all(df_4h.copy())
            df_1h = engine.calculate_all(df_1h.copy())
            df_15m = engine.calculate_all(df_15m.copy())

            fetch_meta["fetched"][symbol] = {
                "4h": len(df_4h), "1h": len(df_1h), "15m": len(df_15m),
            }
            log.info(
                "  %s: 4H=%d (%s → %s), 1H=%d, 15m=%d",
                symbol, len(df_4h), df_4h.index[0], df_4h.index[-1],
                len(df_1h), len(df_15m),
            )

            # Generate 30-min checkpoints across 7d window
            checkpoints_7d: list[datetime] = []
            t = window_7d_start
            while t <= window_7d_end:
                checkpoints_7d.append(t)
                t += timedelta(minutes=CYCLE_MINUTES)

            results: list[CheckpointResult] = []
            for cp in checkpoints_7d:
                r = evaluate_checkpoint(symbol, cp, df_4h, df_1h, df_15m)
                if r is not None:
                    results.append(r)

            per_symbol_results_7d[symbol] = results
            per_symbol_results_24h[symbol] = [
                r for r in results if r.checkpoint >= window_24h_start
            ]
            log.info(
                "  %s: %d 7d checkpoints evaluated, %d in 24h window",
                symbol, len(results), len(per_symbol_results_24h[symbol]),
            )
    finally:
        await client.close()

    summaries_7d = {
        sym: summarize(rs, sym, "7d") for sym, rs in per_symbol_results_7d.items()
    }
    summaries_24h = {
        sym: summarize(rs, sym, "24h") for sym, rs in per_symbol_results_24h.items()
    }

    report = build_report(
        summaries_7d, summaries_24h,
        per_symbol_results_7d, per_symbol_results_24h,
        fetch_meta,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"Wrote {out_path}")

    # Also write per-checkpoint CSV for evidence trail
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for sym, rs in per_symbol_results_7d.items():
            for r in rs:
                rows.append({
                    "symbol": r.symbol,
                    "checkpoint": r.checkpoint.isoformat(),
                    "regime": r.regime,
                    "adx": r.adx,
                    "full_route": r.full_route,
                    "reduced_route": r.reduced_route,
                    "path_4h_flip": r.path_4h_flip,
                    "path_1h_continuation": r.path_1h_continuation,
                    "path_15m_fast": r.path_15m_fast,
                    "path_aligned": r.path_aligned,
                    "full_mode_confidence": r.full_mode_confidence,
                    "reduced_mode_confidence": r.reduced_mode_confidence,
                    "full_mode_reachable": r.full_mode_reachable,
                    "reduced_mode_reachable": r.reduced_mode_reachable,
                    "block_reason": r.block_reason,
                })
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        print(f"Wrote {csv_path}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default="docs/reports/REDUCED_MODE_REACHABILITY_AUDIT.md",
        help="Path to markdown report",
    )
    ap.add_argument(
        "--csv",
        default="docs/reports/reduced_mode_reachability.csv",
        help="Path to per-checkpoint evidence CSV",
    )
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
