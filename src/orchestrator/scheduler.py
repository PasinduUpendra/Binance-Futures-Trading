"""
Scheduler for orchestrator cycles and periodic tasks.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Coroutine, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("claude_quant.scheduler")


class TradingScheduler:
    """Manages scheduled tasks for the trading system."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._jobs: dict[str, str] = {}  # name -> job_id

    def add_cycle_job(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
        interval_seconds: int = 300,
    ) -> None:
        """Add the main trading cycle (default: every 5 minutes)."""
        job = self._scheduler.add_job(
            callback,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id="main_cycle",
            name="Trading Cycle",
            max_instances=1,  # Never overlap cycles
            misfire_grace_time=60,
        )
        self._jobs["main_cycle"] = job.id
        logger.info(f"Main cycle scheduled every {interval_seconds}s")

    def add_daily_report_job(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
        hour: int = 0,
        minute: int = 5,
    ) -> None:
        """Add daily report job (default: 00:05 UTC)."""
        job = self._scheduler.add_job(
            callback,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="daily_report",
            name="Daily Report",
            max_instances=1,
        )
        self._jobs["daily_report"] = job.id
        logger.info(f"Daily report scheduled at {hour:02d}:{minute:02d} UTC")

    def add_health_check_job(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
        interval_seconds: int = 60,
    ) -> None:
        """Add health check job (default: every 60 seconds)."""
        job = self._scheduler.add_job(
            callback,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id="health_check",
            name="Health Check",
            max_instances=1,
        )
        self._jobs["health_check"] = job.id
        logger.info(f"Health check scheduled every {interval_seconds}s")

    def add_bias_analysis_job(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Add weekly bias analysis job (Sundays at 00:00 UTC)."""
        job = self._scheduler.add_job(
            callback,
            trigger=CronTrigger(day_of_week="sun", hour=0, minute=0),
            id="bias_analysis",
            name="Weekly Bias Analysis",
            max_instances=1,
        )
        self._jobs["bias_analysis"] = job.id
        logger.info("Weekly bias analysis scheduled for Sundays 00:00 UTC")

    def start(self) -> None:
        """Start the scheduler."""
        self._scheduler.start()
        logger.info(f"Scheduler started with {len(self._jobs)} jobs")

    def stop(self) -> None:
        """Stop the scheduler gracefully."""
        self._scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped")

    def pause_trading(self) -> None:
        """Pause the main trading cycle."""
        if "main_cycle" in self._jobs:
            self._scheduler.pause_job("main_cycle")
            logger.warning("Trading cycle PAUSED")

    def resume_trading(self) -> None:
        """Resume the main trading cycle."""
        if "main_cycle" in self._jobs:
            self._scheduler.resume_job("main_cycle")
            logger.info("Trading cycle RESUMED")

    def get_next_run_time(self, job_name: str) -> datetime | None:
        """Get next scheduled run time for a job."""
        job_id = self._jobs.get(job_name)
        if job_id:
            job = self._scheduler.get_job(job_id)
            if job:
                return job.next_run_time
        return None
