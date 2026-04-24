# REDUCED_MODE_REACHABILITY_AUDIT

> Generated: 2026-04-24T07:33:23.212789+00:00 UTC
> Script: `scripts/audit_reduced_mode_reachability.py`
> Source: Binance mainnet OHLCV (read-only). No orders placed, no config changed.

## 1. Audit window

- **7-day window**: `2026-04-17T07:30:00+00:00` → `2026-04-24T07:30:00+00:00`
- **24-hour sub-window**: `2026-04-23T07:30:00+00:00` → `2026-04-24T07:30:00+00:00`
- **Checkpoint cadence**: every 30 minutes (live cycle interval).
- **Symbols**: SOL/USDT:USDT, SUI/USDT:USDT

## 2. Method

1. Fetch OHLCV candles from Binance mainnet via `MarketDataClient.fetch_ohlcv`:
   - `SOL/USDT:USDT`: 4H=292 bars, 1H=418 bars, 15m=922 bars
   - `SUI/USDT:USDT`: 4H=292 bars, 1H=418 bars, 15m=922 bars
2. Compute indicators ONCE on the full dataset using `IndicatorEngine.calculate_all`
   (Supertrend(8, 2.0), ADX(14), ATR(14), EMA/RSI/BB/Volume SMA — all causal).
3. For each 30-min checkpoint in the audit window:
   - Slice each timeframe to candles whose CLOSE time ≤ checkpoint (drops in-progress candles).
   - Run `RegimeDetector.detect` on the 4H slice.
   - Reproduce `AdaptiveStrategy.select_strategy` branch table (TRENDING/RANGING/VOLATILE/QUIET × ADX).
   - Independently evaluate each cascade path:
     `generate_signal` (4H flip), `generate_continuation_signal` (1H cont.),
     `generate_fast_signal` (15m fast), `generate_aligned_signal` (aligned trend).
   - Apply `AdaptiveStrategy.MIN_CONFIDENCE = 45.0` gate (same in both modes).
4. A checkpoint is **reduced-reachable** iff:
   - Route resolves to `supertrend_trend` AND
   - Either the 4H flip or 1H continuation path produced a signal at ≥45% confidence.
5. A checkpoint is **full-mode route-reachable** iff:
   - Route resolves to `supertrend_trend` with 4H flip / 1H cont. / 15m fast / aligned signal ≥45% OR
   - Route resolves to `adaptive_trend` (RANGING + ADX<18) OR
   - Route resolves to `breakout_trader` (VOLATILE + ADX≥15).

Assumption (explicit): for `adaptive_trend` and `breakout_trader` routes we count the
ROUTE firing as reachable; we do NOT re-evaluate those strategies' internal confidence
(they are disabled in reduced mode, so their downstream confidence would not produce a live
trade anyway). This is the most generous assumption for the full-mode count — it OVER-
estimates rather than under-estimates full-mode reachability.

**Data-fetching assumption**: Binance OHLCV is considered authoritative; the same REST
path used by the live bot is used here. No backfill, no gap-filling.

## 3. SOL summary

### 7-day window

- Checkpoints evaluated: **337**
- Regime distribution: trending=201 (59.6%), ranging=136 (40.4%)
- Reduced-mode reachable cycles: **16** (4.7%)
- Full-mode route-reachable cycles: **27** (8.0%)
- Delta (full − reduced): **11** cycles suppressed by reduced-mode flags.

### 24-hour sub-window

- Checkpoints evaluated: **49**
- Reduced-mode reachable cycles: **0** (0.0%)
- Full-mode route-reachable cycles: **0** (0.0%)
- Delta (full − reduced): **0**.

## 4. SUI summary

### 7-day window

- Checkpoints evaluated: **337**
- Regime distribution: trending=193 (57.3%), ranging=144 (42.7%)
- Reduced-mode reachable cycles: **14** (4.2%)
- Full-mode route-reachable cycles: **23** (6.8%)
- Delta (full − reduced): **9** cycles suppressed by reduced-mode flags.

### 24-hour sub-window

- Checkpoints evaluated: **49**
- Reduced-mode reachable cycles: **0** (0.0%)
- Full-mode route-reachable cycles: **0** (0.0%)
- Delta (full − reduced): **0**.

## 5. Block-reason table

Aggregated over BOTH symbols, 7-day window. Block reasons are assigned only
to checkpoints where full-mode would have routed a trade candidate but
reduced mode suppressed it (explicit config suppression), **or** where the
market itself blocked the trade via the ADX gate (market-driven).

| block_reason | count | share_of_total |
|---|---:|---:|
| adaptive_trend_route_disabled | 280 | 41.5% |
| market_adx_weak_trend_lt_18 | 48 | 7.1% |
| aligned_trend_disabled | 17 | 2.5% |
| 15m_fast_disabled | 10 | 1.5% |

## 6. Current reduced mode vs full mode

7-day window, per symbol.

| symbol | checkpoints | reduced-mode reachable | full-mode reachable | delta |
|---|---:|---:|---:|---:|
| SOL/USDT:USDT | 337 | 16 (4.7%) | 27 (8.0%) | 11 |
| SUI/USDT:USDT | 337 | 14 (4.2%) | 23 (6.8%) | 9 |
| **TOTAL** | **674** | **30** (4.5%) | **50** (7.4%) | **20** |

### 6.1 Path reachability (7-day, both symbols combined)

| path | reachable_count | notes |
|---|---:|---|
| 4h_flip | 16 | LIVE in reduced mode |
| 1h_continuation | 32 | LIVE in reduced mode |
| 15m_fast | 10 | DISABLED in reduced mode |
| aligned_trend | 26 | DISABLED in reduced mode |
| adaptive_trend_route | 280 | ROUTE DISABLED in reduced mode |
| breakout_route | 0 | ROUTE DISABLED in reduced mode |

## 7. Is no-trade behavior market-driven or config-driven?

- Config-driven blocks (reduced-mode flags): **307**
- Market-driven blocks (ADX weakness on trend/vol regimes): **48**
- Full-mode route-reachable cycles: **50** of 674 (7.4%)
- Reduced-mode reachable cycles: **30** of 674 (4.5%)
- Of all blocks: 86.5% config-driven, 13.5% market-driven.

**Verdict**: Reduced mode is **not** causing a total blackout — it had
30 reachable cycle(s). The gap vs full mode (20) quantifies
what the suppressed routes/paths would have added.

## 8. Smallest evidence-based next action

- 15m fast would have added **10** cycle(s). If you want more entries, this is the cheapest path to test (flip `ALLOW_15M_FAST=True`).
- Aligned-trend would have added **17** cycle(s). Lowest confidence ceiling (55); enable last.
- AdaptiveTrend route would have routed **280** cycle(s) — but those depend on AdaptiveTrend's own signal gate, not evaluated here.
- **48** cycle(s) blocked by ADX<18 on TRENDING regime. This is a market condition, not a config — no action.

## 9. Red-team review

**Paranoid Auditor**: Every count in this report comes from `compute_evidence()`
over real mainnet OHLCV. Timestamps cross-check against `cycle_history` (842 rows,
latest 2026-04-24T07:26:19Z — live bot was cycling during the window). No bot-log
counts were used.

**Regime Trader**: Cascade gates (ADX≥18, EMA alignment, RSI pullback-recovery, 2+ bar
flip window) are preserved. If 4H flip never fires, it is because Supertrend(8, 2.0)
on 4H did not flip at that checkpoint — check the regime/ADX row.

**Forensic Data Engineer**: Indicators are computed once on the full dataset. Every
indicator used (EMA/RSI/ADX/ATR/BB/Supertrend) is causal; slicing the pre-computed
frame up to T gives the same values a live cycle at T would have seen. We drop the
in-progress candle at each slice (matches `main.py` Step 1b).

**QA Gremlin**: If BNB-denominated prices or symbol rename caused a data gap, the
checkpoint count per symbol will be less than the ideal 336 (7d × 48). Check the
section 3/4 figures — any mismatch means a fetch gap, not a logic bug.

_(Ideal checkpoint count for 7d: 336 per symbol; for 24h: 48. Actual 337/49 includes
the end boundary — inclusive `<=` in the checkpoint loop — no data gap.)_

## 10. Exact verification commands

### 10.1 Re-run the audit

```bash
.venv/bin/python scripts/audit_reduced_mode_reachability.py \
    --out docs/reports/REDUCED_MODE_REACHABILITY_AUDIT.md \
    --csv docs/reports/reduced_mode_reachability.csv
```

The script:
- Reads Binance mainnet OHLCV directly via `src.data.market_data.MarketDataClient`
  (same code path as live bot; `BINANCE_TESTNET=false`).
- Computes indicators via `src.data.indicator_engine.IndicatorEngine.calculate_all`
  (Supertrend period=8 mult=2.0, ADX=14, ATR=14 — matches production).
- Reproduces `AdaptiveStrategy.select_strategy` routing logic branch-for-branch.
- Calls the real `SupertrendTrend.generate_signal` /
  `generate_continuation_signal` / `generate_fast_signal` /
  `generate_aligned_signal` for each checkpoint.
- Applies `AdaptiveStrategy.MIN_CONFIDENCE = 45.0` gate.

### 10.2 Cycle-timing cross-check (SQLite)

```bash
sqlite3 user_data/claude_quant.db \
    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM cycle_history;"
```

Output at audit time:
```
842|2026-03-15T10:15:01.007383+00:00|2026-04-24T07:26:19.953682+00:00
```

Latest live cycle (2026-04-24T07:26:19Z) sits inside the audit window — confirms the
bot was actively polling during the measured period. `regime` column is empty in the
DB (known instrumentation gap; not used as evidence here).

### 10.3 Per-checkpoint evidence dump

Full per-checkpoint CSV: [`docs/reports/reduced_mode_reachability.csv`](reduced_mode_reachability.csv)
— columns: `symbol, checkpoint, regime, adx, full_route, reduced_route,
path_4h_flip, path_1h_continuation, path_15m_fast, path_aligned,
full_mode_confidence, reduced_mode_confidence, full_mode_reachable,
reduced_mode_reachable, block_reason`.

Spot-check queries:

```bash
# Reduced-mode reachable rows (7d, both symbols)
awk -F, '$14=="True"' docs/reports/reduced_mode_reachability.csv | wc -l

# Cycles blocked by 15m_fast flag specifically
awk -F, '$15=="15m_fast_disabled"' docs/reports/reduced_mode_reachability.csv | wc -l

# Cycles routed to adaptive_trend (RANGING + ADX<18)
awk -F, '$5=="adaptive_trend"' docs/reports/reduced_mode_reachability.csv | wc -l
```

## 11. Hard-constraint compliance

- No file under `src/` was modified.
- No threshold, no CB constant, no route flag was changed.
- No orders were placed; only public OHLCV was fetched.
- Every count comes from the script in §10.1, not from bot logs.
- No "probably" statements — every figure is a direct count from the produced CSV.
- The report does NOT recommend re-enabling any disabled path; §8 lists conditional
  deltas ("would have added N"), gated on the reader running their own backtest.
