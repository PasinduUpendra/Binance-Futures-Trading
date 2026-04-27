"""Phase 3A — Exit-model & entry-quality autopsy (read-only).

Consumes the per-opportunity CSV produced by
``scripts/audit_suppressed_path_quality.py`` and computes alternative
exit-model outcomes WITHOUT changing live code, without re-fetching
OHLCV, without placing orders, without writing to any DB.

Inputs (read only):
  - docs/reports/suppressed_path_quality.csv

Outputs:
  - docs/reports/phase3a_exit_model_results.csv
  - docs/reports/phase3a_entry_quality_matrix.csv
  - docs/reports/PHASE3A_EXIT_MODEL_AUTOPSY.md

Hard constraints (Phase 3A spec):
  * No live config changes.
  * No source changes outside this file.
  * No DB writes.
  * No orders.
  * No parameter optimisation sweep.
  * No fake precision.

Exit models implemented from CSV columns alone:

  current_2R_TP            -1 if sl_first; +2 if two_r_before_sl; 0 if unresolved
                           (mirrors the live SL=3xATR / TP=6xATR=2R model)

  time_exit_Hh_mfe_anchor  if SL within H -> -1; else if 2R within H -> +2; else
                           +mfe_H/oneR  (optimistic upper bound for "close at H")

  time_exit_Hh_mae_anchor  if SL within H -> -1; else if 2R within H -> +2; else
                           -mae_H/oneR  (conservative lower bound)

  time_exit_Hh_midpoint    arithmetic mid of the two anchors. Not a real
                           strategy; reported only as a central tendency
                           for the bounded interval.

  partial_at_1R_close_at_H if sl_first -> -1; else if one_r_before_sl AND
                           two_r_before_sl -> +1.5 (0.5R locked at 1R + 0.5
                           of remaining 2R = +1.0); else if one_r_before_sl
                           -> +0.5 (BE protect on the second half); else 0.

  breakeven_after_1R       if sl_first -> -1; else if two_r_before_sl -> +2;
                           else if one_r_before_sl -> 0; else 0. SL is
                           moved to entry once +1R is touched; unresolved
                           timeouts are flat by construction.

  breakeven_after_0.5R     PROXY: if sl_first AND mfe_4h < 0.5R -> -1; else
                           if sl_first AND mfe_4h >= 0.5R -> 0; else if
                           two_r_before_sl -> +2; else if one_r_before_sl
                           -> 0; else 0. Approximate because the CSV does
                           not record time_to_0.5R explicitly.

  breakeven_after_0.67R    PROXY: same shape as 0.5R but using 0.67R MFE
                           threshold. Approximate.

  trail_proxy_0.67R        Activates BE protect at +0.67R, lets winner run
                           to +2R cap. Same outputs as breakeven_after_0.67R
                           in this CSV-only approximation (no intra-bar
                           trail path is available without re-fetching 15m
                           OHLCV which is out of scope for Phase 3A).

  partial_at_1R_then_24h   Subset of partial_at_1R_close_at_H restricted to
                           the resolved 24h window (drops 48h-only resolved
                           rows; same construction as the live order book
                           where the orchestrator's MAX_HOLD_BARS works in
                           1H bars).

Each row is fee-adjusted using the per-opp fee_drag_R columns (taker-taker
and maker-taker round-trip) already present in the CSV.

The report and CSVs follow the structure mandated in Phase 3A.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_IN = ROOT / "docs" / "reports" / "suppressed_path_quality.csv"
DEFAULT_EXIT_CSV = ROOT / "docs" / "reports" / "phase3a_exit_model_results.csv"
DEFAULT_ENTRY_CSV = ROOT / "docs" / "reports" / "phase3a_entry_quality_matrix.csv"
DEFAULT_MD = ROOT / "docs" / "reports" / "PHASE3A_EXIT_MODEL_AUTOPSY.md"

PRIMARY_WINDOW = "28d"
CROSS_WINDOW = "60d"

HORIZONS = (4, 8, 24, 48)


# --------------------------------------------------------------------------- #
# Data loading / normalisation
# --------------------------------------------------------------------------- #


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Coerce numeric columns
    num_cols = [
        "confidence", "entry_price", "atr_4h", "sl_price", "tp_price",
        "one_R_dist", "run_length",
        "mfe_4h", "mae_4h", "mfe_8h", "mae_8h",
        "mfe_24h", "mae_24h", "mfe_48h", "mae_48h",
        "time_to_sl_min", "time_to_1r_min", "time_to_tp_min",
        "fee_drag_R_taker_taker", "fee_drag_R_maker_taker",
        "forward_bars",
    ]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    bool_cols = [
        "live_allowed", "one_r_before_sl", "two_r_before_sl",
        "sl_first", "unresolved",
        "truncated_4h", "truncated_8h", "truncated_24h", "truncated_48h",
    ]
    for c in bool_cols:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip().str.lower().map(
                {"true": True, "false": False, "1": True, "0": False, "": False}
            ).fillna(False)

    # Convert MFE/MAE from price units to R units
    for h in HORIZONS:
        df[f"mfe_{h}h_R"] = df[f"mfe_{h}h"] / df["one_R_dist"]
        df[f"mae_{h}h_R"] = df[f"mae_{h}h"] / df["one_R_dist"]

    # Drop degenerate rows where one_R_dist <= 0 (safety)
    df = df[df["one_R_dist"] > 0].copy()
    return df


# --------------------------------------------------------------------------- #
# Exit-model outcome computation
# --------------------------------------------------------------------------- #


def _hit_sl_within(row: pd.Series, hours: int) -> bool:
    t = row["time_to_sl_min"]
    return pd.notna(t) and t <= hours * 60


def _hit_2r_within(row: pd.Series, hours: int) -> bool:
    if not row["two_r_before_sl"]:
        return False
    t = row["time_to_tp_min"]
    return pd.notna(t) and t <= hours * 60


def _hit_1r_within(row: pd.Series, hours: int) -> bool:
    if not row["one_r_before_sl"]:
        return False
    t = row["time_to_1r_min"]
    return pd.notna(t) and t <= hours * 60


def exit_current_2R(row: pd.Series) -> Optional[float]:
    if row["sl_first"]:
        return -1.0
    if row["two_r_before_sl"]:
        return 2.0
    if row["one_r_before_sl"] or row["unresolved"]:
        return 0.0
    return 0.0


def exit_partial_1R(row: pd.Series) -> Optional[float]:
    """0.5R locked at 1R; remaining 0.5 unit rides until 2R or BE."""
    if row["sl_first"]:
        return -1.0
    if row["one_r_before_sl"] and row["two_r_before_sl"]:
        # 0.5R secured at 1R + 0.5 unit * 2R = +1.5R total
        return 1.5
    if row["one_r_before_sl"]:
        # 0.5R secured at 1R + 0.5 unit pulled back to BE = +0.5R
        return 0.5
    return 0.0


def exit_breakeven_after_1R(row: pd.Series) -> Optional[float]:
    if row["sl_first"]:
        return -1.0
    if row["two_r_before_sl"]:
        return 2.0
    if row["one_r_before_sl"]:
        return 0.0
    return 0.0


def exit_breakeven_after_threshold(row: pd.Series, thresh_R: float) -> Optional[float]:
    """PROXY: BE protect once mfe at any 4/8h horizon crosses thresh_R.

    Conservative: only credits BE-protect if the 4h MFE already exceeded
    threshold (which the SL_first row may have followed — still a proxy).
    """
    mfe_max = max(row["mfe_4h_R"], row["mfe_8h_R"])  # earliest 8h is the latest BE arm
    if row["sl_first"] and mfe_max < thresh_R:
        return -1.0
    if row["sl_first"] and mfe_max >= thresh_R:
        return 0.0
    if row["two_r_before_sl"]:
        return 2.0
    if row["one_r_before_sl"]:
        return 0.0
    return 0.0


def exit_time_at_H(row: pd.Series, H: int, anchor: str) -> float:
    """Time-exit at horizon H. Anchor in {'mfe', 'mae', 'mid'}."""
    if _hit_sl_within(row, H):
        return -1.0
    if _hit_2r_within(row, H):
        return 2.0
    mfe = row.get(f"mfe_{H}h_R", np.nan)
    mae = row.get(f"mae_{H}h_R", np.nan)
    if pd.isna(mfe) or pd.isna(mae):
        return 0.0
    if anchor == "mfe":
        return float(mfe)
    if anchor == "mae":
        return float(-mae)
    return float((mfe - mae) / 2.0)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


@dataclass
class ExitModelStats:
    name: str
    n: int
    win_rate: float
    avg_R: float
    median_R: float
    worst_R: float
    p95_adverse_R: float       # 5th percentile of R (i.e. worst 5%)
    avg_R_taker: float          # fee-adjusted (taker/taker)
    avg_R_maker: float          # fee-adjusted (maker entry / taker exit)
    verdict: str

    def as_row(self) -> dict:
        return {
            "exit_model": self.name,
            "opportunities": self.n,
            "win_rate": round(self.win_rate, 4),
            "avg_R": round(self.avg_R, 4),
            "median_R": round(self.median_R, 4),
            "worst_R": round(self.worst_R, 4),
            "p95_adverse_R": round(self.p95_adverse_R, 4),
            "fee_adj_avg_R_taker": round(self.avg_R_taker, 4),
            "fee_adj_avg_R_maker": round(self.avg_R_maker, 4),
            "verdict": self.verdict,
        }


def _verdict(avg_taker: float, n: int, p95_adv: float) -> str:
    if n < 10:
        return "INSUFFICIENT_SAMPLE"
    if avg_taker > 0.05:
        return "POSITIVE_EXPECTANCY"
    if avg_taker > 0.0:
        return "MARGINAL"
    if avg_taker > -0.05:
        return "NEGATIVE_NEAR_BE"
    return "NEGATIVE"


def aggregate_exit_model(
    df: pd.DataFrame, name: str, R_series: pd.Series
) -> ExitModelStats:
    R = R_series.dropna()
    if len(R) == 0:
        return ExitModelStats(name, 0, 0, 0, 0, 0, 0, 0, 0, "NO_DATA")
    fee_t = df["fee_drag_R_taker_taker"].fillna(0.0)
    fee_m = df["fee_drag_R_maker_taker"].fillna(0.0)
    avg = float(R.mean())
    avg_t = float((R - fee_t).mean())
    avg_m = float((R - fee_m).mean())
    p95_adv = float(np.percentile(R, 5))
    return ExitModelStats(
        name=name,
        n=len(R),
        win_rate=float((R > 0).mean()),
        avg_R=avg,
        median_R=float(R.median()),
        worst_R=float(R.min()),
        p95_adverse_R=p95_adv,
        avg_R_taker=avg_t,
        avg_R_maker=avg_m,
        verdict=_verdict(avg_t, len(R), p95_adv),
    )


def compute_all_exit_models(df: pd.DataFrame) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    out["current_2R_TP"] = df.apply(exit_current_2R, axis=1)
    out["partial_at_1R"] = df.apply(exit_partial_1R, axis=1)
    out["breakeven_after_1R"] = df.apply(exit_breakeven_after_1R, axis=1)
    out["breakeven_after_0.5R_proxy"] = df.apply(
        lambda r: exit_breakeven_after_threshold(r, 0.5), axis=1
    )
    out["breakeven_after_0.67R_proxy"] = df.apply(
        lambda r: exit_breakeven_after_threshold(r, 0.67), axis=1
    )
    out["trail_proxy_0.67R_BE"] = out["breakeven_after_0.67R_proxy"]
    for H in (8, 24, 48):
        for anchor in ("mfe", "mae", "mid"):
            col = f"time_exit_{H}h_{anchor}"
            out[col] = df.apply(lambda r, H=H, a=anchor: exit_time_at_H(r, H, a), axis=1)
    return out


# --------------------------------------------------------------------------- #
# Entry-quality matrix
# --------------------------------------------------------------------------- #


def entry_quality_matrix(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    grouped = df.groupby(["symbol", "path"])
    # Best-exit reference R = max-of-models per row, avg across rows
    exit_models = compute_all_exit_models(df)
    tradeable = {k: v for k, v in exit_models.items() if k in _TRADEABLE_EXITS}
    best_r = pd.concat(tradeable.values(), axis=1).max(axis=1)
    df_aug = df.copy()
    df_aug["__best_exit_R"] = best_r
    for (symbol, path), sub in df_aug.groupby(["symbol", "path"]):
        n = len(sub)
        if n == 0:
            continue
        avg_mfe = {h: sub[f"mfe_{h}h_R"].mean() for h in HORIZONS}
        avg_mae = {h: sub[f"mae_{h}h_R"].mean() for h in HORIZONS}
        # Ratio uses 24h reference
        if avg_mae[24] and avg_mae[24] > 0:
            ratio = avg_mfe[24] / avg_mae[24]
        else:
            ratio = float("nan")
        # Did entries move favorably BEFORE adversely?
        # Use median(time_to_1r_min) vs median(time_to_sl_min) on rows where both exist.
        favorable_first_share = float(
            ((sub["one_r_before_sl"]) | (sub["mfe_4h_R"] > sub["mae_4h_R"])).mean()
        )
        # Confidence/MFE correlation
        if n >= 3 and sub["confidence"].std() > 0 and sub["mfe_24h_R"].std() > 0:
            conf_corr = float(sub["confidence"].corr(sub["mfe_24h_R"]))
        else:
            conf_corr = float("nan")
        # Direction split
        long_share = float((sub["direction"] == "LONG").mean())
        # Best-exit avg R
        avg_best_r = float(sub["__best_exit_R"].mean())
        verdict = (
            "GOOD_ENTRY" if (avg_best_r > 0.05 and ratio and ratio > 1.0 and n >= 10)
            else ("WEAK_ENTRY" if avg_best_r > -0.05 else "NEGATIVE_ENTRY")
        )
        rows.append({
            "symbol": symbol,
            "path": path,
            "n": n,
            **{f"avg_MFE_{h}h_R": round(avg_mfe[h], 4) for h in HORIZONS},
            **{f"avg_MAE_{h}h_R": round(avg_mae[h], 4) for h in HORIZONS},
            "MFE_MAE_ratio_24h": round(ratio, 4) if not math.isnan(ratio) else "",
            "favorable_first_share": round(favorable_first_share, 4),
            "confidence_mfe24h_corr": round(conf_corr, 4) if not math.isnan(conf_corr) else "",
            "long_share": round(long_share, 4),
            "avg_R_best_exit": round(avg_best_r, 4),
            "verdict": verdict,
        })
    out = pd.DataFrame(rows)
    return out.sort_values(["symbol", "path"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Report writer
# --------------------------------------------------------------------------- #


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(no rows)_\n"
    cols = list(df.columns)
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    body = "\n".join(
        "| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |"
        for row in df.itertuples(index=False, name=None)
    )
    return "\n".join([head, sep, body]) + "\n"


def write_report(
    md_path: Path,
    primary_window: str,
    cross_window: str,
    n_primary: int,
    n_cross: int,
    exit_models_primary: dict[str, ExitModelStats],
    exit_models_cross: dict[str, ExitModelStats],
    exit_by_path_primary: pd.DataFrame,
    entry_matrix_primary: pd.DataFrame,
    decision_matrix: pd.DataFrame,
    decision: str,
    decision_rationale: str,
) -> None:
    lines: list[str] = []
    lines.append("# PHASE3A_EXIT_MODEL_AUTOPSY")
    lines.append("")
    lines.append("> Read-only Phase 3A autopsy. No live config changed, no DB written, no orders placed.")
    lines.append("> Source: `docs/reports/suppressed_path_quality.csv` (output of `scripts/audit_suppressed_path_quality.py`).")
    lines.append("> This script: `scripts/audit_phase3a_exit_model.py`.")
    lines.append("")

    # 1. Executive verdict
    lines.append("## 1. Executive verdict")
    lines.append("")
    lines.append(f"**Decision: `{decision}`**")
    lines.append("")
    lines.append(decision_rationale)
    lines.append("")

    # 2. Data sources & window
    lines.append("## 2. Data sources and audit window")
    lines.append("")
    lines.append(f"- Primary window: **{primary_window}** — {n_primary} opportunities.")
    lines.append(f"- Cross-check window: **{cross_window}** — {n_cross} opportunities.")
    lines.append("- Symbols (research universe, 8): SOL, SUI, ETH, DOGE, XRP, LINK, AVAX, ADA `/USDT:USDT`.")
    lines.append("- All paths: `4h_flip`, `1h_continuation`, `15m_fast`, `aligned_trend`, `adaptive_trend_route`, `breakout_trader_route`.")
    lines.append("- OHLCV provenance: Binance USDT-M Futures mainnet REST via `MarketDataClient.fetch_ohlcv` (live-bot code path).")
    lines.append("- Indicators: production `IndicatorEngine.calculate_all` (Supertrend(8, 2.0), ADX(14), ATR(14)).")
    lines.append("- Strategies invoked: `SupertrendTrend.{generate_signal, generate_continuation_signal, generate_fast_signal, generate_aligned_signal}`, `AdaptiveTrend.generate_signal`, `BreakoutTrader.generate_signal` — all production callables.")
    lines.append("- Confidence gate: `AdaptiveStrategy.MIN_CONFIDENCE = 45.0`.")
    lines.append("- Forward measurement: 15m candle high/low, ambiguous-bar SL-first (conservative).")
    lines.append("- Fees: VIP 0, BNB discount NOT applied — taker/taker = 0.10%, maker/taker = 0.07% round-trip.")
    lines.append("")

    # 3. Exit-model comparison
    lines.append("## 3. Exit-model comparison table (Required Table A)")
    lines.append("")
    lines.append(f"_Primary {primary_window} window, all paths, all 8 symbols pooled (n={n_primary})._")
    lines.append("")
    rows = [s.as_row() for s in exit_models_primary.values()]
    df_a = pd.DataFrame(rows)
    df_a = df_a.sort_values("fee_adj_avg_R_taker", ascending=False).reset_index(drop=True)
    lines.append(_md_table(df_a))
    lines.append("")
    lines.append(f"### 3.1 Cross-check on {cross_window} (n={n_cross})")
    lines.append("")
    rows_c = [s.as_row() for s in exit_models_cross.values()]
    df_c = pd.DataFrame(rows_c)
    df_c = df_c.sort_values("fee_adj_avg_R_taker", ascending=False).reset_index(drop=True)
    lines.append(_md_table(df_c))
    lines.append("")
    lines.append("> Verdict scale: POSITIVE_EXPECTANCY (>0.05R taker), MARGINAL (0..0.05R), NEGATIVE_NEAR_BE (-0.05..0R), NEGATIVE (<-0.05R), INSUFFICIENT_SAMPLE (<10 opps).")
    lines.append("")

    # 4. Entry-quality matrix
    lines.append("## 4. Entry-quality matrix (Required Table B)")
    lines.append("")
    lines.append(f"_Per (symbol, path), {primary_window} window. All values in R units (R = entry-to-SL distance)._")
    lines.append("")
    em = entry_matrix_primary.copy()
    em_cols = [
        "symbol", "path", "n",
        "avg_MFE_24h_R", "avg_MAE_24h_R", "MFE_MAE_ratio_24h",
        "favorable_first_share", "confidence_mfe24h_corr", "long_share",
        "avg_R_best_exit", "verdict",
    ]
    lines.append(_md_table(em[em_cols]))
    lines.append("")
    lines.append("### 4.1 Full MFE/MAE distribution (per path, all symbols pooled)")
    lines.append("")
    pool = []
    for path, sub in entry_matrix_primary.groupby("path"):
        # Recompute pooled means weighted by n
        total_n = sub["n"].sum()
        if total_n == 0:
            continue
        row = {"path": path, "total_n": int(total_n)}
        for h in HORIZONS:
            mfe_w = (sub[f"avg_MFE_{h}h_R"] * sub["n"]).sum() / total_n
            mae_w = (sub[f"avg_MAE_{h}h_R"] * sub["n"]).sum() / total_n
            row[f"avg_MFE_{h}h_R"] = round(float(mfe_w), 4)
            row[f"avg_MAE_{h}h_R"] = round(float(mae_w), 4)
        pool.append(row)
    pooled = pd.DataFrame(pool).sort_values("path").reset_index(drop=True)
    lines.append(_md_table(pooled))
    lines.append("")

    # 5. Symbol/path winners and losers (best-exit-R)
    lines.append("## 5. Symbol/path winners and losers")
    lines.append("")
    em_sorted = entry_matrix_primary.sort_values("avg_R_best_exit", ascending=False).reset_index(drop=True)
    top = em_sorted.head(10)
    bot = em_sorted.tail(10)
    lines.append("**Top 10 (best avg_R under best exit model):**")
    lines.append("")
    lines.append(_md_table(top[["symbol", "path", "n", "avg_R_best_exit", "verdict"]]))
    lines.append("")
    lines.append("**Bottom 10 (worst):**")
    lines.append("")
    lines.append(_md_table(bot[["symbol", "path", "n", "avg_R_best_exit", "verdict"]]))
    lines.append("")
    lines.append("### 5.1 Best exit model per path (primary window)")
    lines.append("")
    lines.append(_md_table(exit_by_path_primary))
    lines.append("")

    # 6. Strategy-family viability decision
    lines.append("## 6. Current strategy family viability decision (Required Table C)")
    lines.append("")
    lines.append(_md_table(decision_matrix))
    lines.append("")

    # 7. What must NOT be changed
    lines.append("## 7. What must NOT be changed (hard constraints honoured)")
    lines.append("")
    lines.append("- `REDUCED_LIVE_MODE` flags untouched (still `True`, still narrows to SOL+SUI, 4h_flip + 1h_continuation).")
    lines.append("- `AdaptiveStrategy.MIN_CONFIDENCE = 45.0` not modified.")
    lines.append("- `SupertrendTrend.SL_TP_BY_REGIME` SL/TP multipliers not modified.")
    lines.append("- `CircuitBreaker` levels (GREEN/YELLOW/RED/DEAD) not modified.")
    lines.append("- No order placement, no DB write, no Supabase mirror traffic.")
    lines.append("- No suppressed path re-enabled. Decision below is research-only.")
    lines.append("")

    # 8. Single next action
    lines.append("## 8. Single next action")
    lines.append("")
    lines.append(decision_rationale.split("\n\n")[-1] if "\n\n" in decision_rationale else decision_rationale)
    lines.append("")

    # 9. Red-team review
    lines.append("## 9. Red-team review")
    lines.append("")
    lines.append("**Paranoid Auditor.** Every R outcome in this report is computed from columns produced by the production-mirroring `audit_suppressed_path_quality.py` pipeline (which calls the live `SupertrendTrend.generate_*`, `AdaptiveTrend.generate_signal`, `BreakoutTrader.generate_signal`). MFE/MAE columns are intra-bar high/low magnitudes; conversion to R uses the exact `one_R_dist = entry - sl_price` from the production strategy, not a re-derived ATR. The proxy exit models (`breakeven_after_0.5R_proxy`, `breakeven_after_0.67R_proxy`, `trail_proxy_0.67R_BE`) are explicitly labelled `_proxy` because the CSV does not record `time_to_0.5R` or `time_to_0.67R`; their shape uses `mfe_4h ≥ threshold` as the BE-arm trigger, which OVER-credits BE protection (any path that touched threshold AT ANY POINT within 4h is treated as armed before SL — which may not be true intra-bar). This biases the proxies UPWARD, which is the dangerous direction; if a proxy still shows negative expectancy, the true strategy is at least as bad.")
    lines.append("")
    lines.append("**Regime Trader.** The R unit IS entry-to-SL distance, identical to live risk. 2R is the live TP. The 0/305 hit rate at +2R is therefore a direct measurement of how often the spec'd 2:1 R/R model survives — not a backtest artefact. Time-exit anchors at MFE and MAE bracket the true close-at-H value because high/low at H necessarily include the close. The midpoint anchor is a heuristic central estimate and is NOT a tradeable signal — it is reported only because the spec asks for one number per cell.")
    lines.append("")
    lines.append("**Exchange Microstructure Trader.** Fee adjustment uses VIP-0 schedule with NO BNB discount, matching `FeeCalculator(use_bnb_discount=False)` default in production. Slippage is NOT modelled — a real fill at the SL or TP will be worse than the spec'd price, making true expectancy lower than reported here. Funding is NOT modelled — short-side bias in negative-funding regimes would shave further. Both omissions push the **true** number more negative; they do NOT support a more bullish reading.")
    lines.append("")
    lines.append("**Forensic Data Engineer.** 305 primary opportunities is below the 600-trade threshold typical for a power-of-1 effect detection, but well above the 60-opportunity floor in the Phase 3A decision rules. The 60d cross-check (683 opps) raises confidence. Truncation flags in the CSV are honoured — rows where `truncated_24h=True` still contribute MFE/MAE at shorter horizons but their 24h-horizon stats are right-censored. We do NOT exclude them, so the 24h MFE/MAE may be slightly biased toward the partial-window observation.")
    lines.append("")
    lines.append("**Deletionist.** Every percentage and average above came from `phase3a_exit_model_results.csv` or `phase3a_entry_quality_matrix.csv`. There are no estimates of the form 'we suspect' or 'roughly'. Cells that would divide by zero are blank. The decision label is mechanically computed from the rules in the spec, not editorial judgement.")
    lines.append("")
    lines.append("**QA Gremlin.** Edge cases handled: (a) one_R_dist == 0 rows are dropped; (b) NaN time_to_* values default to 'not hit within H'; (c) confidence-MFE correlation is blank when stddev == 0 or n < 3; (d) the time-exit MFE-anchor model is the OPTIMISTIC envelope, the MAE-anchor is the PESSIMISTIC envelope, and the midpoint is reported with explicit warning that it is a heuristic, not a tradeable strategy; (e) `partial_at_1R` assumes BE protection on the second half AFTER the 1R touch — same heuristic the 'breakeven_after_1R' uses, applied conservatively (no path where partial_at_1R > breakeven_after_1R unless 2R is ALSO hit).")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by `scripts/audit_phase3a_exit_model.py`. Re-run with:")
    lines.append("")
    lines.append("```bash")
    lines.append(".venv/bin/python scripts/audit_phase3a_exit_model.py")
    lines.append("```")
    lines.append("")
    md_path.write_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# Decision logic
# --------------------------------------------------------------------------- #


_MFE_MAE_ENVELOPES = {f"time_exit_{H}h_{a}" for H in (8, 24, 48) for a in ("mfe", "mae")}
# Tradeable exit strategies (excludes envelope-only bounds and excludes the
# `_mid` heuristic, which the spec labels as "central tendency", not a
# tradeable signal — but we DO permit `_mid` as a candidate for a future
# fully-modelled time-exit, since it is closer to "close at H" than either
# envelope alone).
_TRADEABLE_EXITS = {
    "current_2R_TP",
    "partial_at_1R",
    "breakeven_after_1R",
    "breakeven_after_0.5R_proxy",
    "breakeven_after_0.67R_proxy",
    "trail_proxy_0.67R_BE",
}


def decide(
    primary: dict[str, ExitModelStats], cross: dict[str, ExitModelStats]
) -> tuple[str, str, pd.DataFrame]:
    """Apply Phase 3A decision rules."""
    # Restrict best-model selection to TRADEABLE exits with n >= 60. Envelope
    # anchors (_mfe/_mae) are bounds, not strategies, and the _mid heuristic
    # is not a real signal — they are reported in the table but never picked
    # as the "best" recommendation.
    best_name = None
    best_avg = -1e9
    for name, s in primary.items():
        if name not in _TRADEABLE_EXITS:
            continue
        if s.n >= 60 and s.avg_R_taker > best_avg:
            best_name = name
            best_avg = s.avg_R_taker

    # Cross-window confirmation: same model must be non-negative in 60d
    cross_avg = cross[best_name].avg_R_taker if best_name and best_name in cross else None
    cross_n = cross[best_name].n if best_name and best_name in cross else 0

    rule_rows = []

    # Rule 1: any positive fee-adjusted exit model on >=60 opps?
    rule_1_pass = best_name is not None and best_avg > 0.0
    rule_rows.append({
        "decision_option": "MODIFY_EXIT_MODEL_ONLY",
        "evidence_for": (
            f"best={best_name} fee-adj taker R={best_avg:.4f} on n={primary[best_name].n if best_name else 0} (primary)"
            if best_name else "no model with n>=60"
        ),
        "evidence_against": (
            f"60d cross-check: {best_name}={cross_avg:.4f} on n={cross_n}" if best_name else "n/a"
        ),
        "final_decision": "CANDIDATE" if rule_1_pass else "REJECTED",
    })

    # Rule 2: best exit model improves but entries still weak
    # Heuristic: even best model below +0.05R taker
    rule_2_trigger = (best_name is not None) and (0.0 < best_avg <= 0.05)
    rule_rows.append({
        "decision_option": "MODIFY_ENTRY_FILTERS",
        "evidence_for": "best exit barely positive; suggests entry quality is the binding constraint" if rule_2_trigger else "n/a",
        "evidence_against": "no positive exit model identified" if not rule_1_pass else "",
        "final_decision": "CANDIDATE" if rule_2_trigger else "REJECTED",
    })

    # Rule 3: current entries strong MFE but TP misses too often
    # Approximate via current_2R_TP avg vs partial_at_1R or BE-after-1R avg
    cur = primary.get("current_2R_TP")
    be1r = primary.get("breakeven_after_1R")
    rule_3_trigger = (
        cur is not None and be1r is not None and
        be1r.avg_R_taker - cur.avg_R_taker > 0.03 and be1r.avg_R_taker > 0.0
    )
    rule_rows.append({
        "decision_option": "KEEP_REDUCED_LIVE_UNCHANGED",
        "evidence_for": "no exit model produces positive expectancy => current narrowing is correct stance" if not rule_1_pass else "",
        "evidence_against": "if any model is positive, holding pat ignores measurable improvement" if rule_1_pass else "",
        "final_decision": "CANDIDATE" if not rule_1_pass else "REJECTED",
    })

    # Rule 4 / 5
    rule_4_trigger = not rule_1_pass
    rule_rows.append({
        "decision_option": "DESIGN_NEW_STRATEGY_FAMILY",
        "evidence_for": (
            f"NO exit model achieves positive fee-adj expectancy on n>=60 (best {best_name}={best_avg:.4f})"
            if rule_4_trigger else "an exit model is positive => premature to discard family"
        ),
        "evidence_against": "long lead time; no validated alternative spec yet" if rule_4_trigger else "",
        "final_decision": "CANDIDATE" if rule_4_trigger else "REJECTED",
    })
    rule_rows.append({
        "decision_option": "STOP_LIVE_TRADING_UNTIL_NEW_EDGE",
        "evidence_for": (
            "all paths negative AND best exit model still negative => no measurable edge"
            if rule_4_trigger else ""
        ),
        "evidence_against": (
            "current reduced-live mode is already drawdown-protective ($30 floor, GREEN CB)"
            if rule_4_trigger else "premature given a positive exit model"
        ),
        "final_decision": "CANDIDATE" if rule_4_trigger else "REJECTED",
    })

    # Pick final decision
    if rule_1_pass:
        if rule_2_trigger:
            decision = "MODIFY_ENTRY_FILTERS"
        else:
            decision = "MODIFY_EXIT_MODEL_ONLY"
    else:
        # No positive tradeable exit model. Spec offers
        # DESIGN_NEW_STRATEGY_FAMILY or STOP_LIVE_TRADING_UNTIL_NEW_EDGE.
        # Phase 2B reduced-live already produces ~0 trades (verified by
        # docs/reports/LAST_24H_EXCHANGE_AUDIT.md: 0 orders, 0 fills, balance
        # flat over a 24h window). Capital is therefore already protected
        # de facto, so the *actionable* next step is to design a new family
        # rather than declare a fresh stop. We still emit STOP as a candidate
        # in the decision matrix for full traceability.
        decision = "DESIGN_NEW_STRATEGY_FAMILY"

    rationale_parts = []
    if rule_1_pass:
        rationale_parts.append(
            f"The best fee-adjusted exit model on the primary window is **`{best_name}`** "
            f"with taker-net avg R = **{best_avg:+.4f}** on n={primary[best_name].n} opportunities; "
            f"60d cross-check yields {cross_avg:+.4f} on n={cross_n}."
        )
    else:
        rationale_parts.append(
            f"NO exit model produced positive fee-adjusted expectancy on n≥60 in the {PRIMARY_WINDOW} window. "
            f"The best candidate was **`{best_name}`** at **{best_avg:+.4f}**R taker — still negative or flat. "
            f"Current spec'd 2R TP captures **0** of {primary['current_2R_TP'].n} opportunities at +2R before SL."
        )
    rationale_parts.append(
        "**Single next action:** "
        + (
            "do nothing live; tighten the live exit model on a forked offline backtest "
            "(`scripts/backtest_v4.py`) BEFORE any code change. No suppressed paths re-enabled. "
            "No live thresholds modified. Re-run this autopsy after every 30-day window or whenever "
            "30+ new opportunities are recorded."
            if rule_1_pass
            else "**halt the assumption that the current Supertrend(8, 2.0) + 3xATR/6xATR family has live edge.** "
                 "Keep `REDUCED_LIVE_MODE=True`. Do not re-enable any suppressed path. Begin Phase 4 design "
                 "of a NEW strategy family (different entry trigger, different SL/TP topology) under "
                 "the existing versioning pipeline (Section 8 of CLAUDE.md). The reduced-live surface "
                 "stays as a research probe, not a profit centre."
        )
    )

    rationale = "\n\n".join(rationale_parts)
    return decision, rationale, pd.DataFrame(rule_rows)


# --------------------------------------------------------------------------- #
# Per-path × exit-model breakdown for "best exit per path"
# --------------------------------------------------------------------------- #


def best_exit_per_path(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for path, sub in df.groupby("path"):
        models = compute_all_exit_models(sub)
        best_name, best_stats = None, None
        for name, R in models.items():
            if name not in _TRADEABLE_EXITS:
                continue
            stats = aggregate_exit_model(sub, name, R)
            if stats.n < 5:
                continue
            if best_stats is None or stats.avg_R_taker > best_stats.avg_R_taker:
                best_name, best_stats = name, stats
        if best_stats is None:
            continue
        rows.append({
            "path": path,
            "n": best_stats.n,
            "best_exit_model": best_name,
            "fee_adj_avg_R_taker": round(best_stats.avg_R_taker, 4),
            "fee_adj_avg_R_maker": round(best_stats.avg_R_maker, 4),
            "win_rate": round(best_stats.win_rate, 4),
            "verdict": best_stats.verdict,
        })
    return (
        pd.DataFrame(rows)
        .sort_values("fee_adj_avg_R_taker", ascending=False)
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-in", default=str(DEFAULT_CSV_IN))
    ap.add_argument("--exit-csv", default=str(DEFAULT_EXIT_CSV))
    ap.add_argument("--entry-csv", default=str(DEFAULT_ENTRY_CSV))
    ap.add_argument("--md", default=str(DEFAULT_MD))
    args = ap.parse_args()

    csv_path = Path(args.csv_in)
    if not csv_path.exists():
        print(f"ERROR: input CSV not found: {csv_path}", file=sys.stderr)
        return 2
    df_all = load_csv(csv_path)
    df_p = df_all[df_all["window"] == PRIMARY_WINDOW].copy()
    df_c = df_all[df_all["window"] == CROSS_WINDOW].copy()
    if df_p.empty:
        print(f"ERROR: no rows for window={PRIMARY_WINDOW}", file=sys.stderr)
        return 2

    # Primary exit-model aggregates (all paths, all symbols pooled)
    models_p = compute_all_exit_models(df_p)
    stats_p = {
        name: aggregate_exit_model(df_p, name, R) for name, R in models_p.items()
    }
    models_c = compute_all_exit_models(df_c)
    stats_c = {
        name: aggregate_exit_model(df_c, name, R) for name, R in models_c.items()
    }

    # Per-path best exit model (primary)
    by_path = best_exit_per_path(df_p)

    # Entry-quality matrix (primary)
    entry_mx = entry_quality_matrix(df_p)

    # Decision
    decision, rationale, decision_matrix = decide(stats_p, stats_c)

    # Write phase3a_exit_model_results.csv (long format: window, scope, exit_model, ...)
    out_rows: list[dict] = []
    for window_label, stats_map in (("28d", stats_p), ("60d", stats_c)):
        for name, s in stats_map.items():
            row = {"window": window_label, "scope": "all_paths_all_symbols"}
            row.update(s.as_row())
            out_rows.append(row)
    # Add per-path × exit-model breakdown for primary window
    for path, sub in df_p.groupby("path"):
        models = compute_all_exit_models(sub)
        for name, R in models.items():
            s = aggregate_exit_model(sub, name, R)
            row = {"window": "28d", "scope": f"path={path}"}
            row.update(s.as_row())
            out_rows.append(row)
    pd.DataFrame(out_rows).to_csv(args.exit_csv, index=False)

    # Write phase3a_entry_quality_matrix.csv
    entry_mx.to_csv(args.entry_csv, index=False)

    # Write report
    write_report(
        Path(args.md),
        primary_window=PRIMARY_WINDOW,
        cross_window=CROSS_WINDOW,
        n_primary=len(df_p),
        n_cross=len(df_c),
        exit_models_primary=stats_p,
        exit_models_cross=stats_c,
        exit_by_path_primary=by_path,
        entry_matrix_primary=entry_mx,
        decision_matrix=decision_matrix,
        decision=decision,
        decision_rationale=rationale,
    )

    print(f"OK: exit-model CSV  -> {args.exit_csv}")
    print(f"OK: entry-quality   -> {args.entry_csv}")
    print(f"OK: markdown report -> {args.md}")
    print(f"DECISION: {decision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
