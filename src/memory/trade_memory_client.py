"""
Trade memory client for Claude Quant.

Provides a unified interface for trade memory, wrapping the TradeMemory
MCP protocol when available and falling back to the local SQLite-backed
TradeJournal when the MCP server is unreachable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from src.memory.trade_journal import TradeEntry, TradeJournal

logger = logging.getLogger("claude_quant.memory.trade_memory_client")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class MemoryQuery(BaseModel):
    """Query parameters for recalling trade memories."""

    query: str = ""
    symbol: str | None = None
    strategy: str | None = None
    regime: str | None = None
    min_confidence: Decimal | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = 20


class BehavioralInsight(BaseModel):
    """A single behavioral insight from trade analysis."""

    category: str
    description: str
    severity: str = Field(
        default="info",
        description="'info', 'warning', or 'critical'",
    )
    evidence: str = ""
    recommendation: str = ""


class BehavioralAnalysis(BaseModel):
    """Full behavioral analysis report."""

    total_trades_analyzed: int = 0
    insights: list[BehavioralInsight] = Field(default_factory=list)
    overall_assessment: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )


# ---------------------------------------------------------------------------
# Custom JSON encoder for Decimal
# ---------------------------------------------------------------------------


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


# ---------------------------------------------------------------------------
# TradeMemoryClient
# ---------------------------------------------------------------------------


class TradeMemoryClient:
    """Unified trade memory client with MCP fallback to local SQLite.

    Tries to communicate with a TradeMemory MCP server via HTTP. If the
    server is unavailable, all operations fall back to the local
    ``TradeJournal`` SQLite database seamlessly.

    Parameters
    ----------
    mcp_url : str | None
        URL of the TradeMemory MCP server (e.g. ``"http://localhost:8765"``).
        If None, only local storage is used.
    journal : TradeJournal | None
        Optional pre-initialized TradeJournal for local fallback.
    timeout : float
        HTTP request timeout in seconds for MCP calls (default 10).
    """

    def __init__(
        self,
        mcp_url: str | None = None,
        journal: TradeJournal | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._mcp_url = mcp_url
        self._timeout = timeout
        self._mcp_available: bool | None = None  # lazy-checked
        self._journal = journal or TradeJournal()
        self._http_client: httpx.AsyncClient | None = None

        logger.info(
            "TradeMemoryClient initialized mcp_url=%s fallback=SQLite",
            self._mcp_url or "NONE (local only)",
        )

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Initialize the HTTP client and check MCP availability."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout)

        if self._mcp_url:
            await self._check_mcp_health()
        else:
            self._mcp_available = False
            logger.info("No MCP URL configured — using local SQLite only")

    async def close(self) -> None:
        """Close the HTTP client and journal."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._journal.close()
        logger.info("TradeMemoryClient closed")

    async def __aenter__(self) -> TradeMemoryClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- MCP health ----------------------------------------------------------

    async def _check_mcp_health(self) -> bool:
        """Check if the MCP server is reachable."""
        if not self._mcp_url or self._http_client is None:
            self._mcp_available = False
            return False

        try:
            response = await self._http_client.get(f"{self._mcp_url}/health")
            self._mcp_available = response.status_code == 200
            if self._mcp_available:
                logger.info("MCP TradeMemory server is available at %s", self._mcp_url)
            else:
                logger.warning(
                    "MCP server responded with status %d — falling back to local",
                    response.status_code,
                )
        except (httpx.ConnectError, httpx.TimeoutException, Exception) as exc:
            self._mcp_available = False
            logger.warning(
                "MCP server unreachable (%s) — falling back to local SQLite",
                exc,
            )

        return self._mcp_available or False

    async def _ensure_client(self) -> None:
        """Ensure the HTTP client is initialized."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout)

    @property
    def is_mcp_available(self) -> bool:
        """Whether the MCP server is currently available."""
        return self._mcp_available is True

    # -- MCP JSON-RPC helper -------------------------------------------------

    async def _mcp_call(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Make a JSON-RPC style call to the MCP server.

        Returns the result dict on success, or None on failure (triggering
        local fallback).
        """
        if not self._mcp_available or not self._mcp_url:
            return None

        await self._ensure_client()
        assert self._http_client is not None

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }

        try:
            response = await self._http_client.post(
                f"{self._mcp_url}/rpc",
                content=json.dumps(payload, cls=_DecimalEncoder),
                headers={"Content-Type": "application/json"},
            )
            if response.status_code == 200:
                data = response.json()
                if "error" in data:
                    logger.warning(
                        "MCP call %s returned error: %s",
                        method,
                        data["error"],
                    )
                    return None
                return data.get("result", {})
            else:
                logger.warning(
                    "MCP call %s returned status %d",
                    method,
                    response.status_code,
                )
                return None
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "MCP call %s failed (%s) — will use local fallback",
                method,
                exc,
            )
            self._mcp_available = False
            return None
        except Exception as exc:
            logger.error("MCP call %s unexpected error: %s", method, exc)
            return None

    # -- public API ----------------------------------------------------------

    async def remember_trade(self, trade_data: TradeEntry | dict[str, Any]) -> bool:
        """Record a trade to memory (MCP server + local SQLite).

        Parameters
        ----------
        trade_data : TradeEntry | dict
            Trade data to record. If a dict, it will be converted to TradeEntry.

        Returns
        -------
        bool
            True if the trade was successfully recorded.
        """
        if isinstance(trade_data, dict):
            entry = TradeEntry(**trade_data)
        else:
            entry = trade_data

        # Always persist locally
        self._journal.record_trade(entry)
        logger.info(
            "REMEMBER_TRADE local trade_id=%s symbol=%s",
            entry.trade_id,
            entry.symbol,
        )

        # Also try MCP if available
        if self._mcp_available:
            result = await self._mcp_call(
                "remember_trade",
                {"trade_data": json.loads(
                    entry.model_dump_json()
                )},
            )
            if result is not None:
                logger.info(
                    "REMEMBER_TRADE mcp_synced trade_id=%s", entry.trade_id
                )
            else:
                logger.warning(
                    "REMEMBER_TRADE mcp_sync_failed trade_id=%s "
                    "(local record intact)",
                    entry.trade_id,
                )

        return True

    async def recall_memories(
        self,
        query: str = "",
        filters: MemoryQuery | dict[str, Any] | None = None,
    ) -> list[TradeEntry]:
        """Recall trade memories matching query and filters.

        Parameters
        ----------
        query : str
            Free-text search query.
        filters : MemoryQuery | dict | None
            Structured filters for the query.

        Returns
        -------
        list[TradeEntry]
            Matching trade entries.
        """
        if isinstance(filters, dict):
            mem_query = MemoryQuery(query=query, **filters)
        elif filters is not None:
            mem_query = filters
            if query:
                mem_query.query = query
        else:
            mem_query = MemoryQuery(query=query)

        # Try MCP first
        if self._mcp_available:
            result = await self._mcp_call(
                "recall_memories",
                {
                    "query": mem_query.query,
                    "filters": json.loads(mem_query.model_dump_json()),
                },
            )
            if result is not None and "trades" in result:
                trades = [TradeEntry(**t) for t in result["trades"]]
                logger.info(
                    "RECALL_MEMORIES mcp count=%d query='%s'",
                    len(trades),
                    mem_query.query,
                )
                return trades

        # Fallback to local SQLite
        trades = self._recall_local(mem_query)
        logger.info(
            "RECALL_MEMORIES local count=%d query='%s'",
            len(trades),
            mem_query.query,
        )
        return trades

    async def get_behavioral_analysis(self) -> BehavioralAnalysis:
        """Get behavioral analysis of trading patterns.

        Attempts to use MCP server's analysis capabilities, falling back
        to a basic local analysis if unavailable.

        Returns
        -------
        BehavioralAnalysis
            Behavioral insights and recommendations.
        """
        # Try MCP first
        if self._mcp_available:
            result = await self._mcp_call("get_behavioral_analysis", {})
            if result is not None:
                try:
                    analysis = BehavioralAnalysis(**result)
                    logger.info(
                        "GET_BEHAVIORAL_ANALYSIS mcp insights=%d",
                        len(analysis.insights),
                    )
                    return analysis
                except Exception as exc:
                    logger.warning(
                        "Failed to parse MCP behavioral analysis: %s", exc
                    )

        # Fallback: basic local analysis
        return self._local_behavioral_analysis()

    # -- local fallback methods ----------------------------------------------

    def _recall_local(self, query: MemoryQuery) -> list[TradeEntry]:
        """Recall trades from local SQLite based on query parameters."""
        # Start with all trades
        trades = self._journal.get_all_trades()

        # Apply filters
        if query.symbol:
            trades = [t for t in trades if t.symbol == query.symbol]

        if query.strategy:
            trades = [t for t in trades if t.strategy == query.strategy]

        if query.regime:
            trades = [t for t in trades if t.regime == query.regime]

        if query.min_confidence is not None:
            trades = [
                t for t in trades if t.confidence >= query.min_confidence
            ]

        if query.date_from:
            trades = [t for t in trades if t.timestamp >= query.date_from]

        if query.date_to:
            trades = [t for t in trades if t.timestamp <= query.date_to]

        # Apply text search on reasoning and lessons fields
        if query.query:
            search_lower = query.query.lower()
            trades = [
                t for t in trades
                if search_lower in t.reasoning.lower()
                or search_lower in t.lessons.lower()
                or search_lower in t.symbol.lower()
                or search_lower in t.strategy.lower()
            ]

        # Sort by timestamp descending and apply limit
        trades.sort(key=lambda t: t.timestamp, reverse=True)
        return trades[: query.limit]

    def _local_behavioral_analysis(self) -> BehavioralAnalysis:
        """Perform basic behavioral analysis using local trade data."""
        trades = self._journal.get_all_trades()
        trades_with_pnl = [t for t in trades if t.pnl is not None]

        insights: list[BehavioralInsight] = []

        if len(trades_with_pnl) < 5:
            return BehavioralAnalysis(
                total_trades_analyzed=len(trades_with_pnl),
                insights=[
                    BehavioralInsight(
                        category="data",
                        description="Insufficient trade history for meaningful analysis",
                        severity="info",
                        recommendation="Continue trading to build analysis data",
                    )
                ],
                overall_assessment="Not enough data for behavioral analysis yet.",
            )

        # Check consecutive losses
        consecutive_losses = self._journal.get_consecutive_losses()
        if consecutive_losses >= 3:
            insights.append(
                BehavioralInsight(
                    category="loss_streak",
                    description=f"Currently on a {consecutive_losses}-trade losing streak",
                    severity="warning" if consecutive_losses < 5 else "critical",
                    evidence=f"{consecutive_losses} consecutive trades with negative PnL",
                    recommendation="Consider reducing position sizes or taking a break",
                )
            )

        # Check win rate
        win_rate = self._journal.get_win_rate()
        if win_rate < 0.4 and len(trades_with_pnl) >= 10:
            insights.append(
                BehavioralInsight(
                    category="low_win_rate",
                    description=f"Win rate is {win_rate:.1%}, below 40% threshold",
                    severity="warning",
                    evidence=f"Win rate calculated from {len(trades_with_pnl)} trades",
                    recommendation="Review strategy selection and entry criteria",
                )
            )

        # Check if cutting winners short and holding losers
        winners = [t for t in trades_with_pnl if t.pnl is not None and t.pnl > 0]
        losers = [t for t in trades_with_pnl if t.pnl is not None and t.pnl < 0]

        if winners and losers:
            avg_win_duration = (
                sum(t.duration for t in winners if t.duration is not None)
                / max(sum(1 for t in winners if t.duration is not None), 1)
            )
            avg_loss_duration = (
                sum(t.duration for t in losers if t.duration is not None)
                / max(sum(1 for t in losers if t.duration is not None), 1)
            )

            if avg_loss_duration > 0 and avg_win_duration > 0:
                if avg_loss_duration > avg_win_duration * 2:
                    insights.append(
                        BehavioralInsight(
                            category="anchoring",
                            description="Holding losing trades significantly longer than winners",
                            severity="warning",
                            evidence=(
                                f"Avg winner duration: {avg_win_duration:.0f}s, "
                                f"avg loser duration: {avg_loss_duration:.0f}s"
                            ),
                            recommendation="Enforce stricter stop-loss discipline",
                        )
                    )

        # Overall assessment
        if not insights:
            overall = "No significant behavioral concerns detected."
        elif any(i.severity == "critical" for i in insights):
            overall = "Critical behavioral issues detected. Review and adjust immediately."
        elif any(i.severity == "warning" for i in insights):
            overall = "Some behavioral patterns warrant attention."
        else:
            overall = "Trading behavior appears generally healthy."

        analysis = BehavioralAnalysis(
            total_trades_analyzed=len(trades_with_pnl),
            insights=insights,
            overall_assessment=overall,
        )

        logger.info(
            "LOCAL_BEHAVIORAL_ANALYSIS trades=%d insights=%d assessment='%s'",
            len(trades_with_pnl),
            len(insights),
            overall,
        )
        return analysis
