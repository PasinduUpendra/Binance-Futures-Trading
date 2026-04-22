"""
Trade journal with SQLite persistence.

Records every trade with full context (strategy, regime, reasoning) and
provides querying/analytics. All monetary values use Decimal.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.memory.trade_journal")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TradeEntry(BaseModel):
    """Complete record of a single trade."""

    trade_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    symbol: str
    direction: str = Field(description="'long' or 'short'")
    entry_price: Decimal
    exit_price: Decimal | None = None
    size: Decimal
    leverage: int = 1
    pnl: Decimal | None = None
    pnl_pct: Decimal | None = None
    strategy: str = ""
    regime: str = ""
    confidence: Decimal = Decimal("0")
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    duration: float | None = Field(
        default=None, description="Trade duration in seconds"
    )
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    reasoning: str = ""
    lessons: str = ""
    mode: str = Field(
        default="",
        description="Trading mode: 'testnet' or 'mainnet'",
    )
    signal_tag: str = Field(
        default="",
        description="Signal origin tag: e.g. '4h_flip', '1h_continuation', 'aligned_trend'",
    )
    exit_reason: str = Field(
        default="",
        description="Exit reason: e.g. 'sl_hit', 'tp_hit', 'trailing_stop', 'reversal', 'time_exit'",
    )
    # Phase 1B: per-trade attribution fields
    cascade_level: str = ""
    confidence_bucket: str = ""
    regime_at_entry: str = ""
    atr_at_entry: str = "0"
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    maker_entry: int = 0
    maker_exit: int = 0
    fees_usd: str = "0"
    funding_usd: str = "0"
    hold_bars: int = 0
    exit_reason_enum: str = ""
    consensus_adj: float = 0.0
    funding_adj: float = 0.0


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    exit_price TEXT,
    size TEXT NOT NULL,
    leverage INTEGER NOT NULL DEFAULT 1,
    pnl TEXT,
    pnl_pct TEXT,
    strategy TEXT DEFAULT '',
    regime TEXT DEFAULT '',
    confidence TEXT DEFAULT '0',
    stop_loss TEXT,
    take_profit TEXT,
    duration REAL,
    fees TEXT DEFAULT '0',
    slippage TEXT DEFAULT '0',
    reasoning TEXT DEFAULT '',
    lessons TEXT DEFAULT '',
    mode TEXT DEFAULT '',
    signal_tag TEXT DEFAULT '',
    exit_reason TEXT DEFAULT '',
    cascade_level TEXT DEFAULT '',
    confidence_bucket TEXT DEFAULT '',
    regime_at_entry TEXT DEFAULT '',
    atr_at_entry TEXT DEFAULT '0',
    entry_slippage_bps REAL DEFAULT 0,
    exit_slippage_bps REAL DEFAULT 0,
    maker_entry INTEGER DEFAULT 0,
    maker_exit INTEGER DEFAULT 0,
    fees_usd TEXT DEFAULT '0',
    funding_usd TEXT DEFAULT '0',
    hold_bars INTEGER DEFAULT 0,
    exit_reason_enum TEXT DEFAULT '',
    consensus_adj REAL DEFAULT 0,
    funding_adj REAL DEFAULT 0
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);
"""

_INSERT_TRADE = """
INSERT OR REPLACE INTO trades (
    trade_id, timestamp, symbol, direction, entry_price, exit_price,
    size, leverage, pnl, pnl_pct, strategy, regime, confidence,
    stop_loss, take_profit, duration, fees, slippage, reasoning, lessons,
    mode, signal_tag, exit_reason,
    cascade_level, confidence_bucket, regime_at_entry, atr_at_entry,
    entry_slippage_bps, exit_slippage_bps, maker_entry, maker_exit,
    fees_usd, funding_usd, hold_bars, exit_reason_enum,
    consensus_adj, funding_adj
) VALUES (
    :trade_id, :timestamp, :symbol, :direction, :entry_price, :exit_price,
    :size, :leverage, :pnl, :pnl_pct, :strategy, :regime, :confidence,
    :stop_loss, :take_profit, :duration, :fees, :slippage, :reasoning, :lessons,
    :mode, :signal_tag, :exit_reason,
    :cascade_level, :confidence_bucket, :regime_at_entry, :atr_at_entry,
    :entry_slippage_bps, :exit_slippage_bps, :maker_entry, :maker_exit,
    :fees_usd, :funding_usd, :hold_bars, :exit_reason_enum,
    :consensus_adj, :funding_adj
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal_to_str(val: Decimal | None) -> str | None:
    """Convert Decimal to string for SQLite storage."""
    if val is None:
        return None
    return str(val)


def _str_to_decimal(val: str | None) -> Decimal | None:
    """Convert SQLite stored string back to Decimal."""
    if val is None:
        return None
    return Decimal(val)


def _row_to_trade_entry(row: dict[str, Any]) -> TradeEntry:
    """Convert a SQLite row dict to a TradeEntry model."""
    return TradeEntry(
        trade_id=row["trade_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        symbol=row["symbol"],
        direction=row["direction"],
        entry_price=Decimal(row["entry_price"]),
        exit_price=_str_to_decimal(row.get("exit_price")),
        size=Decimal(row["size"]),
        leverage=row["leverage"],
        pnl=_str_to_decimal(row.get("pnl")),
        pnl_pct=_str_to_decimal(row.get("pnl_pct")),
        strategy=row.get("strategy", ""),
        regime=row.get("regime", ""),
        confidence=Decimal(row.get("confidence", "0")),
        stop_loss=_str_to_decimal(row.get("stop_loss")),
        take_profit=_str_to_decimal(row.get("take_profit")),
        duration=row.get("duration"),
        fees=Decimal(row.get("fees", "0")),
        slippage=Decimal(row.get("slippage", "0")),
        reasoning=row.get("reasoning", ""),
        lessons=row.get("lessons", ""),
        mode=row.get("mode", ""),
        signal_tag=row.get("signal_tag", ""),
        exit_reason=row.get("exit_reason", ""),
        # Phase 1B attribution fields (default-safe for old rows)
        cascade_level=row.get("cascade_level", "") or "",
        confidence_bucket=row.get("confidence_bucket", "") or "",
        regime_at_entry=row.get("regime_at_entry", "") or "",
        atr_at_entry=row.get("atr_at_entry", "0") or "0",
        entry_slippage_bps=float(row.get("entry_slippage_bps") or 0),
        exit_slippage_bps=float(row.get("exit_slippage_bps") or 0),
        maker_entry=int(row.get("maker_entry") or 0),
        maker_exit=int(row.get("maker_exit") or 0),
        fees_usd=row.get("fees_usd", "0") or "0",
        funding_usd=row.get("funding_usd", "0") or "0",
        hold_bars=int(row.get("hold_bars") or 0),
        exit_reason_enum=row.get("exit_reason_enum", "") or "",
        consensus_adj=float(row.get("consensus_adj") or 0),
        funding_adj=float(row.get("funding_adj") or 0),
    )


# ---------------------------------------------------------------------------
# TradeJournal
# ---------------------------------------------------------------------------


class TradeJournal:
    """Persistent trade journal backed by SQLite.

    Parameters
    ----------
    db_path : str | Path | None
        Path to the SQLite database file. Defaults to
        ``user_data/agent_state/trade_journal.db``.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            db_dir = project_root / "user_data" / "agent_state"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "trade_journal.db"

        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._mirror: Any | None = None  # SupabaseMirror; attach via attach_mirror()
        self._initialize_db()

        logger.info("TradeJournal initialized at %s", self._db_path)

    def attach_mirror(self, mirror: Any) -> None:
        """Attach a non-blocking Supabase mirror. Failures never raise."""
        self._mirror = mirror

    def _mirror_enqueue(self, payload: dict[str, Any]) -> None:
        if self._mirror is None:
            return
        try:
            self._mirror.enqueue("trades", payload)
        except Exception as exc:  # noqa: BLE001 — never block local write
            logger.warning("Trade mirror enqueue failed: %s", exc)

    def _initialize_db(self) -> None:
        """Create the database schema if it doesn't exist."""
        conn = self._get_conn()
        conn.executescript(_CREATE_TABLE + _CREATE_INDEXES)
        # Migrate: add columns if missing (older databases)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        _COLUMN_DEFS = [
            ("mode",               "TEXT DEFAULT ''"),
            ("signal_tag",         "TEXT DEFAULT ''"),
            ("exit_reason",        "TEXT DEFAULT ''"),
            ("cascade_level",      "TEXT DEFAULT ''"),
            ("confidence_bucket",  "TEXT DEFAULT ''"),
            ("regime_at_entry",    "TEXT DEFAULT ''"),
            ("atr_at_entry",       "TEXT DEFAULT '0'"),
            ("entry_slippage_bps", "REAL DEFAULT 0"),
            ("exit_slippage_bps",  "REAL DEFAULT 0"),
            ("maker_entry",        "INTEGER DEFAULT 0"),
            ("maker_exit",         "INTEGER DEFAULT 0"),
            ("fees_usd",           "TEXT DEFAULT '0'"),
            ("funding_usd",        "TEXT DEFAULT '0'"),
            ("hold_bars",          "INTEGER DEFAULT 0"),
            ("exit_reason_enum",   "TEXT DEFAULT ''"),
            ("consensus_adj",      "REAL DEFAULT 0"),
            ("funding_adj",        "REAL DEFAULT 0"),
        ]
        added: list[str] = []
        for col_name, col_def in _COLUMN_DEFS:
            if col_name not in columns:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}")
                added.append(col_name)
        if added:
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create the SQLite connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent read performance
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _trade_to_params(self, entry: TradeEntry) -> dict[str, Any]:
        """Convert a TradeEntry to a dict of SQLite bind parameters."""
        return {
            "trade_id": entry.trade_id,
            "timestamp": entry.timestamp.isoformat(),
            "symbol": entry.symbol,
            "direction": entry.direction,
            "entry_price": str(entry.entry_price),
            "exit_price": _decimal_to_str(entry.exit_price),
            "size": str(entry.size),
            "leverage": entry.leverage,
            "pnl": _decimal_to_str(entry.pnl),
            "pnl_pct": _decimal_to_str(entry.pnl_pct),
            "strategy": entry.strategy,
            "regime": entry.regime,
            "confidence": str(entry.confidence),
            "stop_loss": _decimal_to_str(entry.stop_loss),
            "take_profit": _decimal_to_str(entry.take_profit),
            "duration": entry.duration,
            "fees": str(entry.fees),
            "slippage": str(entry.slippage),
            "reasoning": entry.reasoning,
            "lessons": entry.lessons,
            "mode": entry.mode,
            "signal_tag": entry.signal_tag,
            "exit_reason": entry.exit_reason,
            "cascade_level": entry.cascade_level,
            "confidence_bucket": entry.confidence_bucket,
            "regime_at_entry": entry.regime_at_entry,
            "atr_at_entry": entry.atr_at_entry,
            "entry_slippage_bps": entry.entry_slippage_bps,
            "exit_slippage_bps": entry.exit_slippage_bps,
            "maker_entry": entry.maker_entry,
            "maker_exit": entry.maker_exit,
            "fees_usd": entry.fees_usd,
            "funding_usd": entry.funding_usd,
            "hold_bars": entry.hold_bars,
            "exit_reason_enum": entry.exit_reason_enum,
            "consensus_adj": entry.consensus_adj,
            "funding_adj": entry.funding_adj,
        }

    # -- public API ----------------------------------------------------------

    def record_trade_entry(self, data: dict[str, Any]) -> str:
        """Record a trade from a raw dict (as produced by the orchestrator).

        Maps orchestrator keys (``pair``, ``direction``, …) to
        :class:`TradeEntry` fields and delegates to :meth:`record_trade`.

        Returns
        -------
        str
            The ``trade_id`` that was persisted.  Callers use this as a FK
            when inserting ``fill_events`` rows.
        """
        # Auto-detect mode from env if not provided
        mode = data.get("mode", "")
        if not mode:
            testnet = os.environ.get("BINANCE_TESTNET", "true").lower()
            mode = "testnet" if testnet == "true" else "mainnet"
        entry = TradeEntry(
            symbol=data.get("pair", data.get("symbol", "")),
            direction=str(data.get("direction", "")),
            entry_price=Decimal(str(data.get("entry_price", 0))),
            size=Decimal(str(data.get("size", 0))),
            leverage=int(data.get("leverage", 1)),
            stop_loss=Decimal(str(data["stop_loss"])) if data.get("stop_loss") else None,
            take_profit=Decimal(str(data["take_profit"])) if data.get("take_profit") else None,
            strategy=str(data.get("strategy", "")),
            regime=str(data.get("regime", "")),
            confidence=Decimal(str(data.get("confidence", 0))),
            mode=mode,
            signal_tag=str(data.get("signal_tag", data.get("trigger", ""))),
            # Phase 1B attribution fields
            cascade_level=str(data.get("cascade_level", "")),
            confidence_bucket=str(data.get("confidence_bucket", "")),
            regime_at_entry=str(data.get("regime_at_entry", "")),
            atr_at_entry=str(data.get("atr_at_entry", "0")),
            entry_slippage_bps=float(data.get("entry_slippage_bps", 0.0)),
            maker_entry=int(data.get("maker_entry", 0)),
            fees_usd=str(data.get("fees_usd", "0")),
            consensus_adj=float(data.get("consensus_adj", 0.0)),
            funding_adj=float(data.get("funding_adj", 0.0)),
        )
        self.record_trade(entry)
        return entry.trade_id

    def record_trade(self, entry: TradeEntry) -> None:
        """Record a trade to the journal.

        Parameters
        ----------
        entry : TradeEntry
            Complete trade entry to persist.
        """
        conn = self._get_conn()
        params = self._trade_to_params(entry)
        conn.execute(_INSERT_TRADE, params)
        conn.commit()
        self._mirror_enqueue(params)

        logger.info(
            "TRADE_RECORDED trade_id=%s symbol=%s direction=%s "
            "entry=%s exit=%s pnl=%s strategy=%s",
            entry.trade_id,
            entry.symbol,
            entry.direction,
            entry.entry_price,
            entry.exit_price,
            entry.pnl,
            entry.strategy,
        )

    def get_recent_trades(self, n: int = 10) -> list[TradeEntry]:
        """Get the most recent N trades.

        Parameters
        ----------
        n : int
            Number of trades to return (default 10).

        Returns
        -------
        list[TradeEntry]
            Most recent trades, ordered by timestamp descending.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (n,)
        )
        rows = cursor.fetchall()
        trades = [_row_to_trade_entry(dict(row)) for row in rows]
        logger.debug("GET_RECENT_TRADES count=%d (requested %d)", len(trades), n)
        return trades

    def get_trades_by_strategy(self, strategy: str) -> list[TradeEntry]:
        """Get all trades for a given strategy.

        Parameters
        ----------
        strategy : str
            Strategy name to filter by.

        Returns
        -------
        list[TradeEntry]
            Trades matching the strategy, ordered by timestamp descending.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp DESC",
            (strategy,),
        )
        rows = cursor.fetchall()
        trades = [_row_to_trade_entry(dict(row)) for row in rows]
        logger.debug(
            "GET_TRADES_BY_STRATEGY strategy=%s count=%d", strategy, len(trades)
        )
        return trades

    def get_trades_by_regime(self, regime: str) -> list[TradeEntry]:
        """Get all trades for a given market regime.

        Parameters
        ----------
        regime : str
            Market regime to filter by.

        Returns
        -------
        list[TradeEntry]
            Trades matching the regime, ordered by timestamp descending.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM trades WHERE regime = ? ORDER BY timestamp DESC",
            (regime,),
        )
        rows = cursor.fetchall()
        trades = [_row_to_trade_entry(dict(row)) for row in rows]
        logger.debug(
            "GET_TRADES_BY_REGIME regime=%s count=%d", regime, len(trades)
        )
        return trades

    def get_win_rate(self, last_n: int | None = None) -> float:
        """Calculate win rate across trades.

        Parameters
        ----------
        last_n : int | None
            If provided, only consider the most recent N trades.
            Otherwise, consider all trades with non-null PnL.

        Returns
        -------
        float
            Win rate as a fraction (0.0 to 1.0). Returns 0.0 if no
            trades with PnL data are found.
        """
        conn = self._get_conn()

        if last_n is not None:
            cursor = conn.execute(
                """
                SELECT pnl FROM trades
                WHERE pnl IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (last_n,),
            )
        else:
            cursor = conn.execute(
                "SELECT pnl FROM trades WHERE pnl IS NOT NULL ORDER BY timestamp DESC"
            )

        rows = cursor.fetchall()
        if not rows:
            return 0.0

        wins = sum(1 for row in rows if Decimal(row["pnl"]) > 0)
        win_rate = wins / len(rows)

        logger.debug(
            "GET_WIN_RATE wins=%d total=%d rate=%.4f last_n=%s",
            wins,
            len(rows),
            win_rate,
            last_n,
        )
        return win_rate

    def get_consecutive_losses(self) -> int:
        """Count the current streak of consecutive losing trades.

        Returns
        -------
        int
            Number of consecutive losses from the most recent trade backwards.
            Returns 0 if the most recent trade is a win or no trades exist.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE pnl IS NOT NULL
            ORDER BY timestamp DESC
            """
        )
        rows = cursor.fetchall()

        consecutive = 0
        for row in rows:
            pnl = Decimal(row["pnl"])
            if pnl < 0:
                consecutive += 1
            else:
                break

        logger.debug("GET_CONSECUTIVE_LOSSES streak=%d", consecutive)
        return consecutive

    def get_all_trades(self) -> list[TradeEntry]:
        """Get all trades in the journal.

        Returns
        -------
        list[TradeEntry]
            All trades ordered by timestamp descending.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC"
        )
        rows = cursor.fetchall()
        return [_row_to_trade_entry(dict(row)) for row in rows]

    def get_trade_count(self) -> int:
        """Get the total number of recorded trades.

        Returns
        -------
        int
            Total trade count.
        """
        conn = self._get_conn()
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM trades")
        row = cursor.fetchone()
        return row["cnt"] if row else 0

    def update_trade_exit(
        self,
        symbol: str,
        exit_price: Decimal,
        pnl: Decimal,
        pnl_pct: Decimal,
        duration: float | None = None,
        fees: Decimal = Decimal("0"),
        reason: str = "",
        hold_bars: int | None = None,
        exit_reason_enum: str = "",
        exit_slippage_bps: float = 0.0,
        maker_exit: int = 0,
    ) -> str | None:
        """Update the most recent open trade for *symbol* with exit data.

        Finds the most recent trade for the symbol that has no ``exit_price``
        and fills in the exit fields.  This links trade entries (placed by
        the orchestrator on execution) to their outcomes (detected on close).

        Parameters
        ----------
        symbol : str
            Trading pair in ccxt format (e.g. ``ETH/USDT:USDT``).
        exit_price : Decimal
            Price at which the position was closed.
        pnl : Decimal
            Realized profit/loss in USDT.
        pnl_pct : Decimal
            PnL as a percentage of margin.
        duration : float | None
            Trade duration in hours.
        fees : Decimal
            Total fees for the round-trip.
        reason : str
            Exit reason (e.g. ``trailing_stop``, ``time_exit``,
            ``sl_tp_fire``).
        hold_bars : int | None
            Number of 1H bars held.  Defaults to ``int(duration)`` if
            *duration* is provided, else 0.
        exit_reason_enum : str
            Canonical exit reason enum value (e.g. 'trail', 'time_exit',
            'reversal', 'sl_hit', 'tp_hit').
        exit_slippage_bps : float
            Exit slippage in basis points (signed; positive = overpaid).
        maker_exit : int
            1 if the exit fill was a maker fill, 0 if taker.

        Returns
        -------
        str | None
            The ``trade_id`` of the updated row, or ``None`` if no matching
            open trade was found (standalone record created instead).
        """
        conn = self._get_conn()

        # Find most recent trade for this symbol with no exit
        cursor = conn.execute(
            """
            SELECT trade_id FROM trades
            WHERE symbol = ? AND exit_price IS NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row is None:
            # No matching open trade — create a standalone exit-only record
            # so the exit data is preserved for P&L tracking.
            logger.warning(
                "UPDATE_TRADE_EXIT: No open trade found for %s — "
                "creating standalone exit record", symbol,
            )
            _hold = hold_bars if hold_bars is not None else (int(duration) if duration else 0)
            standalone = TradeEntry(
                symbol=symbol,
                direction="unknown",
                entry_price=Decimal("0"),
                exit_price=exit_price,
                size=Decimal("0"),
                pnl=pnl,
                pnl_pct=pnl_pct,
                duration=duration,
                fees=fees,
                exit_reason=reason,
                hold_bars=_hold,
                exit_reason_enum=exit_reason_enum,
                exit_slippage_bps=exit_slippage_bps,
                maker_exit=maker_exit,
                mode=os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
                and "testnet" or "mainnet",
            )
            params = self._trade_to_params(standalone)
            conn.execute(_INSERT_TRADE, params)
            conn.commit()
            self._mirror_enqueue(params)
            logger.info(
                "TRADE_EXIT_STANDALONE trade_id=%s symbol=%s pnl=%s reason=%s",
                standalone.trade_id, symbol, pnl, reason,
            )
            return None

        trade_id = row["trade_id"]
        _hold_val = hold_bars if hold_bars is not None else (int(duration) if duration else 0)

        conn.execute(
            """
            UPDATE trades
            SET exit_price        = ?,
                pnl               = ?,
                pnl_pct           = ?,
                duration          = ?,
                fees              = ?,
                lessons           = ?,
                exit_reason       = ?,
                hold_bars         = ?,
                exit_reason_enum  = ?,
                exit_slippage_bps = ?,
                maker_exit        = ?
            WHERE trade_id = ?
            """,
            (
                str(exit_price),
                str(pnl),
                str(pnl_pct),
                duration,
                str(fees),
                reason,
                reason,
                _hold_val,
                exit_reason_enum,
                exit_slippage_bps,
                maker_exit,
                trade_id,
            ),
        )
        conn.commit()
        # Read the full updated row to mirror the complete record upstream.
        updated_cursor = conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,),
        )
        updated_row = updated_cursor.fetchone()
        if updated_row is not None:
            self._mirror_enqueue(dict(updated_row))

        logger.info(
            "TRADE_EXIT_RECORDED trade_id=%s symbol=%s exit=%s pnl=%s reason=%s",
            trade_id, symbol, exit_price, pnl, reason,
        )
        return trade_id
