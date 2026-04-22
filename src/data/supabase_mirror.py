"""
Supabase mirror — non-blocking remote replication for local SQLite writes.

Design goals
============
1. Local SQLite stays the source of truth and the primary execution DB.
2. This mirror is best-effort: callers enqueue payloads; a background
   thread drains the queue and upserts into Supabase via PostgREST.
3. Local writes MUST NOT block on Supabase. ``enqueue()`` is a thread-safe
   non-blocking put with bounded queue.
4. Any network / auth failure is logged as a warning, never raised.
5. If ``SUPABASE_URL`` or ``SUPABASE_SERVICE_KEY`` are unset, the mirror is
   disabled and ``enqueue()`` is a no-op.
6. Idempotent upserts: uses ``Prefer: resolution=merge-duplicates`` so
   re-sending the same primary key replaces the row (no duplicates).

This module deliberately stays loop-agnostic (no asyncio). It is safe to
construct from ``DatabaseManager`` / ``TradeJournal`` / ``DecisionAuditor``
regardless of whether an event loop is running.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover — httpx is in requirements.txt
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger("claude_quant.data.supabase_mirror")

_DEFAULT_QUEUE_SIZE = 10_000
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 2.0


# Table → primary-key column(s) for PostgREST upsert on_conflict= hint.
# PostgREST uses ``Prefer: resolution=merge-duplicates`` together with the
# table's real primary key; for composite keys we pass ``on_conflict`` in
# the query string.
_TABLE_CONFLICT: dict[str, str | None] = {
    "trades": "trade_id",
    "cycle_history": "cycle_number",
    "daily_reports": "report_date",
    "trailing_stops": "symbol",
    "audit_trail": "audit_id",
    "system_state": "key",
    "strategy_metrics": "strategy,regime",
    "decision_log": None,  # append-only; no primary-key conflict
}


class SupabaseMirror:
    """Background queue + HTTP worker that mirrors SQLite writes to Supabase.

    Parameters
    ----------
    url : str | None
        Supabase project URL (e.g. ``https://xxx.supabase.co``). Reads
        ``SUPABASE_URL`` env var if ``None``.
    key : str | None
        Supabase service-role key. Reads ``SUPABASE_SERVICE_KEY`` env var
        if ``None``. Service-role is required to bypass row-level security
        for a trusted backend writer.
    queue_size : int
        Maximum queued rows before ``enqueue()`` drops the oldest item.
    timeout : float
        Per-request HTTP timeout (seconds).
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self.url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.getenv("SUPABASE_SERVICE_KEY", "")
        self.enabled = bool(self.url and self.key and httpx is not None)
        self._timeout = timeout
        self._max_retries = max_retries
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=queue_size
        )
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._client: Any = None

        if not self.enabled:
            if httpx is None:
                logger.warning(
                    "SupabaseMirror disabled: httpx is not installed."
                )
            else:
                logger.info(
                    "SupabaseMirror disabled (SUPABASE_URL / "
                    "SUPABASE_SERVICE_KEY not configured) — local writes "
                    "continue unchanged."
                )
            return

        self._client = httpx.Client(timeout=timeout)
        self._worker_thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="supabase-mirror",
        )
        self._worker_thread.start()
        logger.info("SupabaseMirror started (url=%s)", self.url)

    # ----------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------

    def enqueue(self, table: str, row: dict[str, Any]) -> bool:
        """Enqueue a row for upsert. Returns ``True`` if queued.

        Never blocks. Never raises. If the queue is full, the OLDEST item
        is dropped to make room (preserving most-recent writes).
        """
        if not self.enabled:
            return False
        try:
            self._queue.put_nowait({"table": table, "row": row})
            return True
        except queue.Full:
            # Drop oldest, try once more. At this volume this is a last-
            # resort safety; normal operation stays well under queue_size.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait({"table": table, "row": row})
                logger.warning(
                    "Mirror queue full; dropped oldest item to accept %s",
                    table,
                )
                return True
            except queue.Empty:
                logger.warning("Mirror queue race — dropped %s row", table)
                return False

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker and drain the remaining queue up to *timeout*."""
        if not self.enabled:
            return
        self._stop_event.set()
        # Give the worker a sentinel to unblock ``queue.get()``.
        try:
            self._queue.put_nowait({"table": "__stop__", "row": {}})
        except queue.Full:
            pass
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=timeout)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass

    def qsize(self) -> int:
        """Return current queue depth (mostly for monitoring)."""
        return self._queue.qsize() if self.enabled else 0

    # ----------------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------------

    def _worker(self) -> None:
        """Drain the queue and POST each row to Supabase."""
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item["table"] == "__stop__":
                break
            try:
                self._post(item["table"], item["row"])
            except Exception as exc:  # noqa: BLE001 — never raise from worker
                logger.warning(
                    "Supabase mirror failed for %s: %s",
                    item["table"], exc,
                )
            finally:
                self._queue.task_done()

    def _post(self, table: str, row: dict[str, Any]) -> None:
        """Upsert a single row via PostgREST with retries + backoff."""
        endpoint = f"{self.url}/rest/v1/{table}"
        conflict = _TABLE_CONFLICT.get(table)
        params: dict[str, str] = {}
        if conflict is not None:
            params["on_conflict"] = conflict
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.post(
                    endpoint,
                    json=row,
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self._max_retries - 1:
                    backoff = _DEFAULT_BACKOFF_BASE ** attempt
                    time.sleep(backoff)
        if last_exc is not None:
            raise last_exc


# ---------------------------------------------------------------------------
# Process-wide singleton helpers
# ---------------------------------------------------------------------------


_SINGLETON: SupabaseMirror | None = None
_SINGLETON_LOCK = threading.Lock()


def get_mirror() -> SupabaseMirror:
    """Return the process-wide SupabaseMirror (lazy-initialized)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = SupabaseMirror()
    return _SINGLETON


def reset_mirror() -> None:
    """Reset the singleton (used by tests)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            try:
                _SINGLETON.close()
            except Exception:  # noqa: BLE001
                pass
        _SINGLETON = None
