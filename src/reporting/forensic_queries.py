"""
Canonical forensic SQL queries — Phase 1C.

Implements the 12 queries specified in LIVE_FORENSICS_SPEC.md §4.
Each SQL string is a module-level constant so callers can either execute
them directly or paste them into a sqlite3 shell for manual inspection.

``ForensicQueries`` is a thin read-only wrapper over ``DatabaseManager``.
It never writes to the database and never raises into the caller — on any
error it returns an empty list and logs a warning.

Usage
-----
    from src.data.database import DatabaseManager
    from src.reporting.forensic_queries import ForensicQueries

    db = DatabaseManager()
    fq = ForensicQueries(db)
    rows = fq.per_cascade_expectancy()   # -> list[dict]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.data.database import DatabaseManager

logger = logging.getLogger("claude_quant.reporting.forensic_queries")

# ---------------------------------------------------------------------------
# Query 1: Cascade-level conversion funnel
# ---------------------------------------------------------------------------
Q1_CASCADE_CONVERSION_FUNNEL = """
SELECT
    stage,
    cascade_level,
    outcome,
    COUNT(*) AS n
FROM decision_log
WHERE stage IN (
    'signal_generate',
    'confidence_gate',
    'liquidation_buffer',
    'decision_audit'
)
GROUP BY stage, cascade_level, outcome
ORDER BY stage, outcome, n DESC;
"""

# ---------------------------------------------------------------------------
# Query 2: Per-cascade expectancy
# ---------------------------------------------------------------------------
Q2_PER_CASCADE_EXPECTANCY = """
SELECT
    cascade_level,
    COUNT(*)                                                                  AS n,
    AVG(CAST(pnl AS REAL))                                                    AS avg_pnl,
    SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1.0 ELSE 0 END) / COUNT(*)      AS win_rate,
    SUM(CAST(pnl AS REAL))                                                    AS total_pnl
FROM trades
WHERE pnl IS NOT NULL AND cascade_level != ''
GROUP BY cascade_level
ORDER BY avg_pnl DESC;
"""

# ---------------------------------------------------------------------------
# Query 3: Per-regime expectancy
# ---------------------------------------------------------------------------
Q3_PER_REGIME_EXPECTANCY = """
SELECT
    regime_at_entry,
    COUNT(*)                                                                  AS n,
    AVG(CAST(pnl AS REAL))                                                    AS avg_pnl,
    AVG(CAST(fees_usd AS REAL))                                               AS avg_fees,
    AVG(CAST(funding_usd AS REAL))                                            AS avg_funding,
    SUM(CAST(pnl AS REAL))                                                    AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY regime_at_entry
ORDER BY avg_pnl DESC;
"""

# ---------------------------------------------------------------------------
# Query 4: Maker vs taker P&L
# ---------------------------------------------------------------------------
Q4_MAKER_TAKER_PNL = """
SELECT
    maker_entry,
    COUNT(*)                                                                  AS n,
    AVG(CAST(pnl AS REAL) - CAST(fees_usd AS REAL))                           AS avg_net_pnl,
    AVG(CAST(fees_usd AS REAL))                                               AS avg_fees,
    AVG(CAST(pnl AS REAL))                                                    AS avg_gross_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY maker_entry;
"""

# ---------------------------------------------------------------------------
# Query 5: Slippage cost
# ---------------------------------------------------------------------------
Q5_SLIPPAGE_COST = """
SELECT
    AVG(entry_slippage_bps)                           AS avg_entry_slippage_bps,
    AVG(exit_slippage_bps)                            AS avg_exit_slippage_bps,
    AVG(entry_slippage_bps + exit_slippage_bps)       AS avg_roundtrip_slippage_bps,
    COUNT(*)                                          AS n
FROM trades
WHERE pnl IS NOT NULL;
"""

# ---------------------------------------------------------------------------
# Query 6: Rejection distribution
# ---------------------------------------------------------------------------
Q6_REJECTION_DISTRIBUTION = """
SELECT
    stage,
    reason,
    COUNT(*) AS n
FROM decision_log
WHERE outcome = 'reject'
GROUP BY stage, reason
ORDER BY n DESC
LIMIT 20;
"""

# ---------------------------------------------------------------------------
# Query 7: Exit-reason mix
# ---------------------------------------------------------------------------
Q7_EXIT_REASON_MIX = """
SELECT
    exit_reason_enum,
    COUNT(*)                                                                  AS n,
    AVG(CAST(pnl AS REAL))                                                    AS avg_pnl,
    SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1.0 ELSE 0 END) / COUNT(*)      AS win_rate,
    SUM(CAST(pnl AS REAL))                                                    AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY exit_reason_enum
ORDER BY n DESC;
"""

# ---------------------------------------------------------------------------
# Query 8: Per-symbol P&L with fee/funding drag
# ---------------------------------------------------------------------------
Q8_SYMBOL_PNL_WITH_DRAG = """
SELECT
    symbol,
    COUNT(*)                                                                  AS n,
    SUM(CAST(pnl AS REAL))                                                    AS total_pnl,
    SUM(CAST(fees_usd AS REAL))                                               AS total_fees,
    SUM(CAST(funding_usd AS REAL))                                            AS total_funding,
    SUM(CAST(pnl AS REAL))
        - SUM(CAST(fees_usd AS REAL))
        + SUM(CAST(funding_usd AS REAL))                                      AS gross_edge_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY symbol
ORDER BY total_pnl DESC;
"""

# ---------------------------------------------------------------------------
# Query 9: Confidence-bucket win rate
# ---------------------------------------------------------------------------
Q9_CONFIDENCE_BUCKET_WIN_RATE = """
SELECT
    confidence_bucket,
    COUNT(*)                                                                  AS n,
    SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1.0 ELSE 0 END) / COUNT(*)      AS win_rate,
    AVG(CAST(pnl AS REAL))                                                    AS avg_pnl,
    SUM(CAST(pnl AS REAL))                                                    AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY confidence_bucket
ORDER BY confidence_bucket;
"""

# ---------------------------------------------------------------------------
# Query 10: Funding-filter impact
# ---------------------------------------------------------------------------
Q10_FUNDING_FILTER_IMPACT = """
SELECT
    outcome,
    COUNT(*) AS n
FROM decision_log
WHERE stage = 'funding_filter'
GROUP BY outcome;
"""

# ---------------------------------------------------------------------------
# Query 11: Consensus-adjustment impact on expectancy
# ---------------------------------------------------------------------------
Q11_CONSENSUS_ADJ_IMPACT = """
SELECT
    CASE
        WHEN consensus_adj > 0 THEN 'boosted'
        WHEN consensus_adj < 0 THEN 'penalised'
        ELSE 'neutral'
    END AS bucket,
    COUNT(*)               AS n,
    AVG(CAST(pnl AS REAL)) AS avg_pnl,
    SUM(CAST(pnl AS REAL)) AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY bucket;
"""

# ---------------------------------------------------------------------------
# Query 12: Cycle completion latency (last 14 days)
# ---------------------------------------------------------------------------
Q12_CYCLE_LATENCY = """
SELECT
    DATE(timestamp)        AS d,
    AVG(duration_seconds)  AS avg_seconds,
    MAX(duration_seconds)  AS max_seconds,
    COUNT(*)               AS cycle_count
FROM cycle_history
GROUP BY d
ORDER BY d DESC
LIMIT 14;
"""

# Ordered registry — useful for "run all" loops
ALL_QUERIES: list[tuple[str, str]] = [
    ("q1_cascade_conversion_funnel", Q1_CASCADE_CONVERSION_FUNNEL),
    ("q2_per_cascade_expectancy",    Q2_PER_CASCADE_EXPECTANCY),
    ("q3_per_regime_expectancy",     Q3_PER_REGIME_EXPECTANCY),
    ("q4_maker_taker_pnl",           Q4_MAKER_TAKER_PNL),
    ("q5_slippage_cost",             Q5_SLIPPAGE_COST),
    ("q6_rejection_distribution",    Q6_REJECTION_DISTRIBUTION),
    ("q7_exit_reason_mix",           Q7_EXIT_REASON_MIX),
    ("q8_symbol_pnl_with_drag",      Q8_SYMBOL_PNL_WITH_DRAG),
    ("q9_confidence_bucket_win_rate", Q9_CONFIDENCE_BUCKET_WIN_RATE),
    ("q10_funding_filter_impact",    Q10_FUNDING_FILTER_IMPACT),
    ("q11_consensus_adj_impact",     Q11_CONSENSUS_ADJ_IMPACT),
    ("q12_cycle_latency",            Q12_CYCLE_LATENCY),
]


# ---------------------------------------------------------------------------
# ForensicQueries — read-only executor
# ---------------------------------------------------------------------------


class ForensicQueries:
    """Execute the 12 canonical forensic queries against the live database.

    All methods return a ``list[dict[str, Any]]`` (possibly empty if no data
    exists yet).  No method raises — failures are logged at WARNING level.

    Parameters
    ----------
    db : DatabaseManager
        The canonical database.  Only read operations are performed.
    """

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, sql: str) -> list[dict[str, Any]]:
        """Execute *sql* and return rows as plain dicts."""
        try:
            conn = self._db._get_conn()  # noqa: SLF001 — intentional private access
            cursor = conn.execute(sql)
            return [dict(r) for r in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("ForensicQueries._run failed: %s | sql=%.120s", exc, sql.strip())
            return []

    # ------------------------------------------------------------------
    # Named accessors (one per canonical query)
    # ------------------------------------------------------------------

    def cascade_conversion_funnel(self) -> list[dict[str, Any]]:
        """Q1 — stage × cascade_level × outcome counts from decision_log."""
        return self._run(Q1_CASCADE_CONVERSION_FUNNEL)

    def per_cascade_expectancy(self) -> list[dict[str, Any]]:
        """Q2 — n, avg_pnl, win_rate, total_pnl grouped by cascade_level."""
        return self._run(Q2_PER_CASCADE_EXPECTANCY)

    def per_regime_expectancy(self) -> list[dict[str, Any]]:
        """Q3 — avg_pnl, avg_fees, avg_funding grouped by regime_at_entry."""
        return self._run(Q3_PER_REGIME_EXPECTANCY)

    def maker_taker_pnl(self) -> list[dict[str, Any]]:
        """Q4 — net P&L comparison: maker_entry=1 vs maker_entry=0."""
        return self._run(Q4_MAKER_TAKER_PNL)

    def slippage_cost(self) -> list[dict[str, Any]]:
        """Q5 — avg entry, exit, round-trip slippage in basis points."""
        return self._run(Q5_SLIPPAGE_COST)

    def rejection_distribution(self) -> list[dict[str, Any]]:
        """Q6 — top 20 stage × reason combos for outcome='reject'."""
        return self._run(Q6_REJECTION_DISTRIBUTION)

    def exit_reason_mix(self) -> list[dict[str, Any]]:
        """Q7 — exit_reason_enum counts, avg_pnl, win_rate."""
        return self._run(Q7_EXIT_REASON_MIX)

    def symbol_pnl_with_drag(self) -> list[dict[str, Any]]:
        """Q8 — per-symbol total_pnl, total_fees, total_funding, gross_edge_pnl."""
        return self._run(Q8_SYMBOL_PNL_WITH_DRAG)

    def confidence_bucket_win_rate(self) -> list[dict[str, Any]]:
        """Q9 — win_rate and avg_pnl per confidence_bucket."""
        return self._run(Q9_CONFIDENCE_BUCKET_WIN_RATE)

    def funding_filter_impact(self) -> list[dict[str, Any]]:
        """Q10 — pass/reject/skip counts for the funding_filter stage."""
        return self._run(Q10_FUNDING_FILTER_IMPACT)

    def consensus_adj_impact(self) -> list[dict[str, Any]]:
        """Q11 — boosted / penalised / neutral bucket expectancy."""
        return self._run(Q11_CONSENSUS_ADJ_IMPACT)

    def cycle_latency(self) -> list[dict[str, Any]]:
        """Q12 — daily avg/max cycle duration and count, last 14 days."""
        return self._run(Q12_CYCLE_LATENCY)

    def run_all(self) -> dict[str, list[dict[str, Any]]]:
        """Run all 12 queries and return a dict keyed by query name."""
        return {name: self._run(sql) for name, sql in ALL_QUERIES}
