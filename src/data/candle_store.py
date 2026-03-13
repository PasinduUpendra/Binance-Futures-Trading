"""
SQLite-backed candle storage.

Provides durable, de-duplicated persistence for OHLCV candles with automatic
table creation, upsert on timestamp, and configurable retention cleanup.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.data.candle_store")

# ---------------------------------------------------------------------------
# Pydantic model for candle rows
# ---------------------------------------------------------------------------


class CandleRow(BaseModel):
    """Single candle record used for serialisation in/out of the store."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


# ---------------------------------------------------------------------------
# CandleStore
# ---------------------------------------------------------------------------


class CandleStore:
    """SQLite-backed storage for OHLCV candle data.

    Parameters
    ----------
    db_path : str | Path
        Path to the SQLite database file.  Created automatically if it does
        not yet exist (including parent directories).
    retention_days : int
        Number of days of candle data to retain.  ``cleanup()`` deletes
        anything older.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        retention_days: int = 30,
    ) -> None:
        if db_path is None:
            db_path = os.getenv(
                "CANDLE_STORE_DB_PATH",
                os.path.join("user_data", "candles.db"),
            )
        self._db_path = Path(db_path)
        self._retention_days = retention_days
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open (or create) the SQLite database."""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        # Enable WAL for better concurrent read performance
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        logger.info("CandleStore connected to %s", self._db_path)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("CandleStore closed")

    def __enter__(self) -> CandleStore:
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- helpers -------------------------------------------------------------

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("CandleStore not connected. Call connect() first.")
        return self._conn

    @staticmethod
    def _table_name(symbol: str, timeframe: str) -> str:
        """Derive a safe SQLite table name from symbol + timeframe.

        Example: ``BTC/USDT:USDT`` + ``5m`` -> ``candles_btc_usdt_usdt_5m``
        """
        raw = f"candles_{symbol}_{timeframe}"
        safe = re.sub(r"[^a-zA-Z0-9]", "_", raw).lower()
        # Collapse repeated underscores
        safe = re.sub(r"_+", "_", safe).strip("_")
        return safe

    def _ensure_table(self, table: str) -> None:
        """Create the candle table if it does not exist."""
        conn = self._require_conn()
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                timestamp TEXT PRIMARY KEY,
                open      TEXT NOT NULL,
                high      TEXT NOT NULL,
                low       TEXT NOT NULL,
                close     TEXT NOT NULL,
                volume    TEXT NOT NULL
            )
            """
        )
        conn.commit()

    # -- public API ----------------------------------------------------------

    def store_candles(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
    ) -> int:
        """Upsert candles from a DataFrame into the store.

        Parameters
        ----------
        symbol : str
            E.g. ``"BTC/USDT:USDT"``.
        timeframe : str
            E.g. ``"5m"``.
        df : DataFrame
            Must contain ``timestamp``, ``open``, ``high``, ``low``,
            ``close``, ``volume`` columns.

        Returns
        -------
        int
            Number of rows upserted.
        """
        conn = self._require_conn()
        table = self._table_name(symbol, timeframe)
        self._ensure_table(table)

        # Normalise column names
        work = df.copy()
        work.columns = [c.lower() for c in work.columns]

        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(work.columns)
        if missing:
            raise ValueError(f"DataFrame missing columns: {missing}")

        rows: list[tuple[str, str, str, str, str, str]] = []
        for _, row in work.iterrows():
            ts = row["timestamp"]
            if isinstance(ts, datetime):
                ts_str = ts.astimezone(timezone.utc).isoformat()
            else:
                ts_str = str(ts)

            rows.append((
                ts_str,
                str(row["open"]),
                str(row["high"]),
                str(row["low"]),
                str(row["close"]),
                str(row["volume"]),
            ))

        conn.executemany(
            f"""
            INSERT INTO "{table}" (timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(timestamp) DO UPDATE SET
                open   = excluded.open,
                high   = excluded.high,
                low    = excluded.low,
                close  = excluded.close,
                volume = excluded.volume
            """,
            rows,
        )
        conn.commit()
        logger.info("Stored %d candles for %s/%s", len(rows), symbol, timeframe)
        return len(rows)

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Retrieve candles from the store.

        Parameters
        ----------
        since : datetime | None
            If given, only return candles with ``timestamp >= since``.
        limit : int
            Maximum number of candles to return (most recent first).
        """
        conn = self._require_conn()
        table = self._table_name(symbol, timeframe)
        self._ensure_table(table)

        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            since_str = since.isoformat()
            query = (
                f'SELECT timestamp, open, high, low, close, volume '
                f'FROM "{table}" WHERE timestamp >= ? '
                f'ORDER BY timestamp DESC LIMIT ?'
            )
            cursor = conn.execute(query, (since_str, limit))
        else:
            query = (
                f'SELECT timestamp, open, high, low, close, volume '
                f'FROM "{table}" ORDER BY timestamp DESC LIMIT ?'
            )
            cursor = conn.execute(query, (limit,))

        rows = cursor.fetchall()

        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        records: list[dict[str, Any]] = []
        for ts_str, o, h, l, c, v in rows:
            records.append({
                "timestamp": datetime.fromisoformat(ts_str),
                "open": Decimal(o),
                "high": Decimal(h),
                "low": Decimal(l),
                "close": Decimal(c),
                "volume": Decimal(v),
            })

        df = pd.DataFrame(records)
        # Return in chronological order
        df = df.sort_values("timestamp").reset_index(drop=True)
        logger.debug(
            "Retrieved %d candles for %s/%s", len(df), symbol, timeframe
        )
        return df

    def get_latest_candle(
        self,
        symbol: str,
        timeframe: str,
    ) -> CandleRow | None:
        """Return the most recent candle for *symbol/timeframe*, or ``None``."""
        conn = self._require_conn()
        table = self._table_name(symbol, timeframe)
        self._ensure_table(table)

        cursor = conn.execute(
            f'SELECT timestamp, open, high, low, close, volume '
            f'FROM "{table}" ORDER BY timestamp DESC LIMIT 1'
        )
        row = cursor.fetchone()
        if row is None:
            return None

        ts_str, o, h, l, c, v = row
        return CandleRow(
            timestamp=datetime.fromisoformat(ts_str),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(l),
            close=Decimal(c),
            volume=Decimal(v),
        )

    def cleanup(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> int:
        """Delete candles older than ``retention_days``.

        If *symbol* and *timeframe* are given, only that table is cleaned.
        Otherwise **all** candle tables are cleaned.

        Returns the total number of deleted rows.
        """
        conn = self._require_conn()
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self._retention_days)
        cutoff_str = cutoff.isoformat()

        tables: list[str] = []
        if symbol is not None and timeframe is not None:
            tables.append(self._table_name(symbol, timeframe))
        else:
            # Discover all candle tables
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'candles_%'"
            )
            tables = [row[0] for row in cursor.fetchall()]

        total_deleted = 0
        for table in tables:
            cursor = conn.execute(
                f'DELETE FROM "{table}" WHERE timestamp < ?', (cutoff_str,)
            )
            total_deleted += cursor.rowcount

        conn.commit()
        logger.info(
            "Cleanup: deleted %d rows older than %s across %d tables",
            total_deleted,
            cutoff_str,
            len(tables),
        )
        return total_deleted
