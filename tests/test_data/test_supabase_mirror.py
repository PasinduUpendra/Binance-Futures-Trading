"""Tests for the non-blocking Supabase mirror.

We never hit the real network. A lightweight stub replaces the httpx.Client
so we can assert that enqueue → drain → HTTP POST happens in the expected
shape, and that failures are contained."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from src.data.supabase_mirror import SupabaseMirror, _TABLE_CONFLICT


class _StubResponse:
    def __init__(self, status: int = 200):
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubClient:
    """Thread-safe stub that records every POST and can fail on demand."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.fail_first_n: int = 0
        self.permanent_failure: bool = False

    def post(self, url: str, json: dict, headers: dict, params: dict | None = None):
        with self._lock:
            self.calls.append({
                "url": url,
                "json": json,
                "headers": headers,
                "params": params or {},
            })
            if self.permanent_failure:
                return _StubResponse(500)
            if self.fail_first_n > 0:
                self.fail_first_n -= 1
                return _StubResponse(500)
        return _StubResponse(200)

    def close(self) -> None:
        pass


def _make_mirror(stub: _StubClient | None = None) -> SupabaseMirror:
    """Construct a mirror with a stubbed HTTP client. Always enabled."""
    m = SupabaseMirror(url="https://fake.supabase.co", key="fake-key",
                       timeout=0.5, max_retries=3)
    # Swap in the stub client.
    if stub is not None:
        m._client = stub  # noqa: SLF001 — test seam
    return m


# ---------------------------------------------------------------------------
# Disabled-mode behaviour
# ---------------------------------------------------------------------------


def test_disabled_when_env_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    m = SupabaseMirror()
    assert m.enabled is False
    # enqueue must be a cheap no-op
    assert m.enqueue("trades", {"trade_id": "x"}) is False
    assert m.qsize() == 0
    m.close()


def test_disabled_when_only_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    m = SupabaseMirror()
    assert m.enabled is False
    m.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_enqueue_posts_row_to_postgrest() -> None:
    stub = _StubClient()
    m = _make_mirror(stub)
    assert m.enabled

    m.enqueue("trades", {"trade_id": "abc", "symbol": "ETH/USDT:USDT"})
    # Let the worker drain.
    for _ in range(50):
        if stub.calls:
            break
        time.sleep(0.05)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["url"] == "https://fake.supabase.co/rest/v1/trades"
    assert call["json"] == {"trade_id": "abc", "symbol": "ETH/USDT:USDT"}
    assert call["headers"]["apikey"] == "fake-key"
    assert call["headers"]["Authorization"] == "Bearer fake-key"
    assert "resolution=merge-duplicates" in call["headers"]["Prefer"]
    # trades has a single-column PK → on_conflict=trade_id hint
    assert call["params"].get("on_conflict") == "trade_id"
    m.close()


def test_composite_primary_key_uses_on_conflict_hint() -> None:
    stub = _StubClient()
    m = _make_mirror(stub)
    m.enqueue("strategy_metrics", {"strategy": "ST", "regime": "TRENDING"})
    for _ in range(50):
        if stub.calls:
            break
        time.sleep(0.05)
    assert stub.calls[0]["params"]["on_conflict"] == "strategy,regime"
    m.close()


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_http_failure_never_raises_from_enqueue() -> None:
    stub = _StubClient()
    stub.permanent_failure = True
    m = _make_mirror(stub)
    # Should return True (enqueued) even though every POST will fail.
    assert m.enqueue("trades", {"trade_id": "fail"}) is True
    # Give the worker time to retry and give up.
    time.sleep(0.5)
    # 3 attempts per item means 3 calls recorded.
    assert len(stub.calls) >= 1
    m.close()


def test_retries_then_succeeds() -> None:
    stub = _StubClient()
    stub.fail_first_n = 2  # succeed on the 3rd attempt
    m = _make_mirror(stub)
    m.enqueue("trades", {"trade_id": "r"})
    # First retry sleeps 2^0 = 1s; second retry sleeps 2^1 = 2s → give 4s.
    for _ in range(50):
        if len(stub.calls) >= 3:
            break
        time.sleep(0.1)
    assert len(stub.calls) == 3
    m.close()


# ---------------------------------------------------------------------------
# Queue-full behaviour
# ---------------------------------------------------------------------------


def test_queue_full_drops_oldest() -> None:
    stub = _StubClient()
    # Make the worker hang on every POST so the queue fills.
    slow_event = threading.Event()

    class _SlowStub(_StubClient):
        def post(self, *args, **kwargs):  # type: ignore[override]
            slow_event.wait(timeout=5.0)
            return super().post(*args, **kwargs)

    slow = _SlowStub()
    m = SupabaseMirror(url="https://fake.supabase.co", key="k",
                       queue_size=2, max_retries=1)
    m._client = slow  # noqa: SLF001

    # Fill: worker picks one up, queue holds 2 more → 4th triggers drop-oldest.
    assert m.enqueue("trades", {"trade_id": "a"}) is True
    assert m.enqueue("trades", {"trade_id": "b"}) is True
    assert m.enqueue("trades", {"trade_id": "c"}) is True
    # All accepted, but oldest dropped inside the queue.
    slow_event.set()
    time.sleep(0.2)
    m.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    from src.data import supabase_mirror as mod

    mod.reset_mirror()
    a = mod.get_mirror()
    b = mod.get_mirror()
    assert a is b
    mod.reset_mirror()
    c = mod.get_mirror()
    assert c is not a


# ---------------------------------------------------------------------------
# Table-conflict map is self-consistent
# ---------------------------------------------------------------------------


def test_all_expected_tables_have_conflict_entries() -> None:
    expected = {"trades", "cycle_history", "daily_reports", "trailing_stops",
                "audit_trail", "system_state", "strategy_metrics"}
    assert expected.issubset(_TABLE_CONFLICT.keys())
