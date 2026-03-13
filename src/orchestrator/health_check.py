"""
System health monitoring for all components.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.health_check")


class ComponentHealth(BaseModel):
    """Health status of a single component."""
    name: str
    healthy: bool
    latency_ms: float = 0.0
    last_check: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None


class SystemHealth(BaseModel):
    """Overall system health."""
    healthy: bool
    components: list[ComponentHealth] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    uptime_seconds: float = 0.0

    @property
    def unhealthy_components(self) -> list[str]:
        return [c.name for c in self.components if not c.healthy]


class HealthChecker:
    """Monitors health of all system components."""

    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._last_health: Optional[SystemHealth] = None

    async def check_exchange_connection(self) -> ComponentHealth:
        """Check Binance API connectivity."""
        start = time.monotonic()
        try:
            from src.data.market_data import MarketDataClient
            client = MarketDataClient()
            await client.fetch_ticker("BTC/USDT:USDT")
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(name="exchange", healthy=True, latency_ms=latency)
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="exchange", healthy=False, latency_ms=latency, error=str(e)
            )

    async def check_database(self) -> ComponentHealth:
        """Check SQLite database accessibility."""
        start = time.monotonic()
        try:
            from src.memory.trade_journal import TradeJournal
            journal = TradeJournal()
            journal.get_recent_trades(1)
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(name="database", healthy=True, latency_ms=latency)
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="database", healthy=False, latency_ms=latency, error=str(e)
            )

    async def check_disk_space(self) -> ComponentHealth:
        """Check available disk space."""
        try:
            import shutil
            usage = shutil.disk_usage("/")
            free_gb = usage.free / (1024**3)
            healthy = free_gb > 1.0  # Need at least 1GB free
            return ComponentHealth(
                name="disk_space",
                healthy=healthy,
                error=f"Only {free_gb:.1f}GB free" if not healthy else None,
            )
        except Exception as e:
            return ComponentHealth(name="disk_space", healthy=False, error=str(e))

    async def run_all_checks(self) -> SystemHealth:
        """Run all health checks concurrently."""
        checks = await asyncio.gather(
            self.check_exchange_connection(),
            self.check_database(),
            self.check_disk_space(),
            return_exceptions=True,
        )

        components = []
        for check in checks:
            if isinstance(check, Exception):
                components.append(
                    ComponentHealth(name="unknown", healthy=False, error=str(check))
                )
            else:
                components.append(check)

        uptime = time.monotonic() - self._start_time
        all_healthy = all(c.healthy for c in components)

        health = SystemHealth(
            healthy=all_healthy,
            components=components,
            uptime_seconds=uptime,
        )

        if not all_healthy:
            logger.warning(f"Unhealthy components: {health.unhealthy_components}")

        self._last_health = health
        return health

    @property
    def last_health(self) -> Optional[SystemHealth]:
        return self._last_health
