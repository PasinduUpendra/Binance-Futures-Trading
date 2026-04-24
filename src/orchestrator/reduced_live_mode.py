"""Reduced live mode — single source of truth for the narrowed live surface.

Phase 2B (2026-04-22): the live orchestrator is running on a small balance
($68 mainnet) and the full decision surface (8 pairs × 4 cascade levels ×
3 strategy routes × consensus adjust × dynamic +1 position override) is
larger than what Phase 1 attribution can measure. This module shrinks the
live path to a reversible, auditable minimum.

ROLLBACK
--------
Set ``REDUCED_LIVE_MODE = False`` below. Every consumer short-circuits to
its pre-Phase-2B behavior (no filtering, no gating). No other file needs
to change. See ``docs/PHASE2B_REDUCED_LIVE_MODE.md`` for verification.

WHAT IT CONTROLS
----------------
- Which pairs are iterated in the cycle signal loop (filters ``TRADING_PAIRS``)
- Which SupertrendTrend cascade levels are tried (4H flip + 1H continuation ON;
  15m fast + aligned-trend OFF)
- Which strategy routes inside ``AdaptiveStrategy.select_strategy`` are live
  (SupertrendTrend ON; AdaptiveTrend + BreakoutTrader OFF)
- Whether ``CrossAssetConsensus`` applies its ±10 confidence adjustment
  (OFF — neutralized by returning an empty map)
- Whether the dynamic +1 position override above the circuit-breaker cap
  is applied (OFF — effective cap == CB cap)

WHAT IT DOES NOT TOUCH
----------------------
- Circuit-breaker constants (immutable)
- Confidence / ADX / RSI / leverage / funding thresholds
- SupertrendTrend SL/TP multipliers
- Strategy file contents — disabled code remains on disk for resurrection
- Infra (persistence, mirrors, launchd, heartbeat)

This module MUST stay dependency-free (pure constants + pure helpers) so
it can be imported from any layer without creating an import cycle.
"""

from __future__ import annotations

from typing import Iterable

# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------

#: When ``True``, every helper below enforces the reduced surface. Set to
#: ``False`` to restore full pre-Phase-2B behavior. This is the single
#: rollback point.
REDUCED_LIVE_MODE: bool = True


# ---------------------------------------------------------------------------
# Allowed symbols
# ---------------------------------------------------------------------------

#: The only pairs the orchestrator is allowed to SIGNAL / ENTER on in
#: reduced mode. Existing positions on other pairs are still reconciled
#: and exited normally — we stop *opening* new ones, we do not force-close
#: holdings of previously-allowed pairs. (Confirmed by code review of
#: ``_reconcile_positions_and_orders`` which operates on exchange-reported
#: open positions, not on ``TRADING_PAIRS``.)
ALLOWED_SYMBOLS: frozenset[str] = frozenset({
    "SOL/USDT:USDT",
    "SUI/USDT:USDT",
})


# ---------------------------------------------------------------------------
# Entry-path feature flags
# ---------------------------------------------------------------------------
# Each flag gates one specific code path. Keep True/False literal at module
# scope (no env reads) so that tests can patch deterministically.

#: 4H Supertrend flip entry (`SupertrendTrend.generate_signal`).
ALLOW_4H_FLIP: bool = True

#: 1H continuation entry inside an established 4H trend
#: (`SupertrendTrend.generate_continuation_signal`).
ALLOW_1H_CONTINUATION: bool = True

#: 15m fast-entry cascade level (`SupertrendTrend.generate_fast_signal`).
#: Disabled in reduced mode — 15m window on a 30-min polling cycle is
#: mostly redundant with 1H continuation (see MASTER_ROADMAP Phase 2).
ALLOW_15M_FAST: bool = False

#: Aligned-trend RSI-pullback entry (`SupertrendTrend.generate_aligned_signal`).
#: Disabled in reduced mode — confidence ceiling 55 sits right on the
#: MIN_CONFIDENCE floor (45), producing the narrowest-margin trades.
ALLOW_ALIGNED_TREND: bool = False


# ---------------------------------------------------------------------------
# Strategy-route feature flags
# ---------------------------------------------------------------------------

#: AdaptiveTrend route (RANGING + ADX<18). Disabled — insufficient live
#: evidence; the bot stays flat in that regime until Phase-1 attribution
#: produces a decision.
ALLOW_ADAPTIVE_TREND_ROUTE: bool = False

#: BreakoutTrader route (VOLATILE + ADX≥15). Disabled — historically
#: negative EV per SSOT §4.3 / CHANGELOG.
ALLOW_BREAKOUT_TRADER_ROUTE: bool = False


# ---------------------------------------------------------------------------
# Orchestrator-level feature flags
# ---------------------------------------------------------------------------

#: Cross-asset consensus ±10 pt confidence adjustment. Disabled in reduced
#: mode — adjustment magnitude is smaller than a confidence-tier boundary
#: so it only adds noise until Phase 1 can attribute it.
ALLOW_CONSENSUS_ADJUST: bool = False

#: Dynamic +1 position override above the CB cap in GREEN. Disabled in
#: reduced mode — at current balance a 4th slot often cannot clear the
#: per-pair min-notional floor, and the override is a functional
#: relaxation of Immutable Rule #3.
ALLOW_DYNAMIC_POS_OVERRIDE: bool = False


# ---------------------------------------------------------------------------
# Helpers (pure functions; safe to call from tests and any layer)
# ---------------------------------------------------------------------------


def is_reduced_mode() -> bool:
    """Return whether reduced live mode is currently active."""

    return REDUCED_LIVE_MODE


def filter_trading_pairs(pairs: Iterable[str]) -> list[str]:
    """Return the subset of ``pairs`` allowed under the current mode.

    In full mode (``REDUCED_LIVE_MODE=False``) the input list is returned
    unchanged. In reduced mode, only pairs in :data:`ALLOWED_SYMBOLS` are
    kept, preserving the input ordering. Pairs not in ``pairs`` but in
    ``ALLOWED_SYMBOLS`` are NOT injected — the caller's master list still
    defines the universe.
    """

    pairs_list = list(pairs)
    if not REDUCED_LIVE_MODE:
        return pairs_list
    return [p for p in pairs_list if p in ALLOWED_SYMBOLS]


def is_pair_allowed(symbol: str) -> bool:
    """Return whether ``symbol`` may receive new entry signals."""

    if not REDUCED_LIVE_MODE:
        return True
    return symbol in ALLOWED_SYMBOLS


def is_consensus_adjust_allowed() -> bool:
    """Return whether cross-asset consensus confidence adjustment is live."""

    if not REDUCED_LIVE_MODE:
        return True
    return ALLOW_CONSENSUS_ADJUST


def is_dynamic_pos_override_allowed() -> bool:
    """Return whether the dynamic +1 position override is live."""

    if not REDUCED_LIVE_MODE:
        return True
    return ALLOW_DYNAMIC_POS_OVERRIDE


def is_cascade_level_allowed(level: str) -> bool:
    """Return whether a SupertrendTrend cascade ``level`` may fire.

    Known levels: ``"4h_flip"``, ``"1h_continuation"``, ``"15m_fast"``,
    ``"aligned_trend"``. Unknown levels are allowed (fail-open) so this
    helper cannot silently disable a future cascade addition without a
    deliberate flag entry above.
    """

    if not REDUCED_LIVE_MODE:
        return True
    return {
        "4h_flip":         ALLOW_4H_FLIP,
        "1h_continuation": ALLOW_1H_CONTINUATION,
        "15m_fast":        ALLOW_15M_FAST,
        "aligned_trend":   ALLOW_ALIGNED_TREND,
    }.get(level, True)


def is_strategy_route_allowed(route: str) -> bool:
    """Return whether a top-level strategy ``route`` may be selected.

    Known routes: ``"supertrend_trend"``, ``"adaptive_trend"``,
    ``"breakout_trader"``. Unknown routes fail-open — same reason as
    :func:`is_cascade_level_allowed`.
    """

    if not REDUCED_LIVE_MODE:
        return True
    return {
        "supertrend_trend": True,   # always on (only primary route)
        "adaptive_trend":   ALLOW_ADAPTIVE_TREND_ROUTE,
        "breakout_trader":  ALLOW_BREAKOUT_TRADER_ROUTE,
    }.get(route, True)


__all__ = [
    "REDUCED_LIVE_MODE",
    "ALLOWED_SYMBOLS",
    "ALLOW_4H_FLIP",
    "ALLOW_1H_CONTINUATION",
    "ALLOW_15M_FAST",
    "ALLOW_ALIGNED_TREND",
    "ALLOW_ADAPTIVE_TREND_ROUTE",
    "ALLOW_BREAKOUT_TRADER_ROUTE",
    "ALLOW_CONSENSUS_ADJUST",
    "ALLOW_DYNAMIC_POS_OVERRIDE",
    "is_reduced_mode",
    "filter_trading_pairs",
    "is_pair_allowed",
    "is_consensus_adjust_allowed",
    "is_dynamic_pos_override_allowed",
    "is_cascade_level_allowed",
    "is_strategy_route_allowed",
]
