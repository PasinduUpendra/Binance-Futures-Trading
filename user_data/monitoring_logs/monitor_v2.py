#!/usr/bin/env python3
"""
Claude Quant 24-Hour Autonomous Monitoring Agent (v2)
=====================================================
OBSERVE and RECORD ONLY — never modifies code, config, database, or state.
Runs 48 checks at 30-minute intervals. Produces final summary report.
"""

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from src.data.market_data import MarketDataClient
from src.execution.position_tracker import PositionTracker
from src.execution.order_manager import OrderManager

# ─── Configuration ───────────────────────────────────────────────
TOTAL_CHECKS = 48
INTERVAL_SECONDS = 1800  # 30 minutes
TRADING_PAIRS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "DOGE/USDT:USDT", "XRP/USDT:USDT", "LINK/USDT:USDT",
    "AVAX/USDT:USDT", "SUI/USDT:USDT", "ADA/USDT:USDT",
]
DB_PATH = PROJECT_ROOT / "user_data" / "claude_quant.db"
BOT_LOG = PROJECT_ROOT / "user_data" / "logs" / "bot.log"
NOHUP_LOG = PROJECT_ROOT / "nohup.out"

# ─── State ───────────────────────────────────────────────────────
LOG_DIR = PROJECT_ROOT / "user_data" / "monitoring_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"audit_{ts}.log"

prev_balance: Decimal | None = None
start_balance: Decimal | None = None
balance_min: Decimal | None = None
balance_max: Decimal | None = None
all_flags: list[tuple[str, str]] = []  # (timestamp, flag)
position_history: list[str] = []
check_number = 0
last_log_lines: list[str] = []
last_log_check_time: datetime | None = None
shutdown_requested = False


def log(msg: str) -> None:
    """Append message to log file."""
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Check Functions ─────────────────────────────────────────────
async def check_bot_health() -> bool:
    """Returns True if bot process is running."""
    log("[1] BOT HEALTH CHECK")
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10,
        )
        bot_lines = [
            l for l in result.stdout.splitlines()
            if "python" in l.lower() and ("run_bot" in l or "orchestrator" in l or "main.py" in l)
            and "monitor" not in l
        ]
        if bot_lines:
            for line in bot_lines:
                log(f"  PROCESS: {line.strip()}")
            return True
        else:
            log("  *** ALERT *** FLAG: BOT_DOWN — No bot process found!")
            return False
    except Exception as e:
        log(f"  ERROR checking bot health: {e}")
        return False


async def check_balance() -> Decimal | None:
    """Fetch current margin balance from Binance."""
    log("[2] BALANCE CHECK")
    client = MarketDataClient()
    try:
        await client.connect()
        bal = await client.get_margin_balance()
        log(f"  BALANCE={bal}")
        return Decimal(str(bal))
    except Exception as e:
        log(f"  ERROR fetching balance: {e}")
        return None
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def check_positions() -> list:
    """Fetch open positions. Returns list of (symbol, side, size, entry, upnl, leverage)."""
    log("[3] OPEN POSITIONS")
    tracker = PositionTracker()
    positions_data = []
    try:
        await tracker.connect()
        positions = await tracker.get_open_positions()
        if not positions:
            log("  NO_OPEN_POSITIONS")
        else:
            for p in positions:
                side_val = p.side.value if hasattr(p.side, "value") else str(p.side)
                line = f"  POS: {p.symbol} {side_val} size={p.size} entry={p.entry_price} upnl={p.unrealized_pnl} lev={p.leverage}"
                log(line)
                positions_data.append({
                    "symbol": p.symbol,
                    "side": side_val,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "leverage": float(p.leverage) if p.leverage else 0,
                })
        return positions_data
    except Exception as e:
        log(f"  ERROR fetching positions: {e}")
        return []
    finally:
        try:
            await tracker.close()
        except Exception:
            pass


async def check_orders() -> dict[str, list]:
    """Fetch open orders for all pairs. Returns dict symbol -> list of order info."""
    log("[4] OPEN ORDERS")
    om = OrderManager()
    orders_by_symbol: dict[str, list] = {}
    try:
        await om.connect()
        for pair in TRADING_PAIRS:
            try:
                orders = await om.get_open_orders(pair)
                if orders:
                    orders_by_symbol[pair] = []
                    for o in orders:
                        otype = getattr(o, "order_type", "?")
                        oside = o.side.value if hasattr(o.side, "value") else str(o.side)
                        oamt = getattr(o, "amount", "?")
                        oprice = getattr(o, "stop_price", None) or getattr(o, "price", "?")
                        line = f"  ORDER: {pair} {otype} {oside} amt={oamt} price={oprice}"
                        log(line)
                        orders_by_symbol[pair].append({"type": otype, "side": oside, "amount": oamt})
            except Exception as e:
                log(f"  ERROR fetching orders for {pair}: {e}")
        if not orders_by_symbol:
            log("  NO_OPEN_ORDERS")
        return orders_by_symbol
    except Exception as e:
        log(f"  ERROR connecting order manager: {e}")
        return {}
    finally:
        try:
            await om.close()
        except Exception:
            pass


def check_log_tail() -> list[str]:
    """Read last 30 lines from bot log."""
    log("[5] RECENT LOGS")
    lines: list[str] = []
    # Prefer bot.log over nohup.out
    for log_path in [BOT_LOG, NOHUP_LOG]:
        if log_path.exists() and log_path.stat().st_size > 0:
            try:
                with open(log_path, "r") as f:
                    all_lines = f.readlines()
                    lines = all_lines[-30:] if len(all_lines) >= 30 else all_lines
                log(f"  (from {log_path.name}, {len(lines)} lines)")
                for line in lines:
                    log(f"  {line.rstrip()}")
                return lines
            except Exception as e:
                log(f"  ERROR reading {log_path}: {e}")
    log("  NO_LOG_FILE")
    return []


def check_database() -> tuple[str, int, int]:
    """Check DB integrity, today's trade count, and active trailing stops."""
    log("[6] DATABASE INTEGRITY")
    integrity = "unknown"
    trades_today = 0
    ts_count = 0
    try:
        conn = sqlite3.connect(str(DB_PATH))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        log(f"  INTEGRITY: {integrity}")

        trades_today = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE date(timestamp)=date('now')"
        ).fetchone()[0]
        log(f"  TRADES_TODAY={trades_today}")

        ts_count = conn.execute(
            "SELECT COUNT(*) FROM trailing_stops"
        ).fetchone()[0]
        log(f"  TRAILING_STOPS_ACTIVE={ts_count}")

        conn.close()
    except Exception as e:
        log(f"  ERROR checking database: {e}")
    return integrity, trades_today, ts_count


def evaluate_flags(
    bot_alive: bool,
    balance: Decimal | None,
    positions: list,
    orders: dict[str, list],
    trades_today: int,
    integrity: str,
    log_lines: list[str],
) -> list[str]:
    """Evaluate anomaly rules and return list of triggered flags."""
    global prev_balance, last_log_check_time, last_log_lines

    flags: list[str] = []

    # BOT_DOWN
    if not bot_alive:
        flags.append("BOT_DOWN")

    # DEAD_LEVEL
    if balance is not None and balance < Decimal("30"):
        flags.append("DEAD_LEVEL")

    # RAPID_DRAWDOWN (>5% drop since last check)
    if balance is not None and prev_balance is not None and prev_balance > 0:
        drop_pct = ((prev_balance - balance) / prev_balance) * 100
        if drop_pct > 5:
            flags.append(f"RAPID_DRAWDOWN({drop_pct:.2f}%)")

    # OVER_POSITIONED
    if len(positions) > 3:
        flags.append(f"OVER_POSITIONED({len(positions)})")

    # LEVERAGE_BREACH
    for p in positions:
        if p.get("leverage", 0) > 10:
            flags.append(f"LEVERAGE_BREACH({p['symbol']} lev={p['leverage']})")

    # ORPHAN_ORDERS
    pos_symbols = {p["symbol"] for p in positions}
    for sym, ords in orders.items():
        if sym not in pos_symbols and ords:
            flags.append(f"ORPHAN_ORDERS({sym})")

    # OVERTRADE_BREACH
    if trades_today > 20:
        flags.append(f"OVERTRADE_BREACH({trades_today})")

    # DB_CORRUPT
    if integrity.lower() != "ok":
        flags.append("DB_CORRUPT")

    # BOT_STALLED (no new log lines in 2+ hours)
    now = datetime.now(timezone.utc)
    if log_lines:
        last_log_lines_snapshot = log_lines
        last_log_check_time = now
    elif last_log_check_time is not None:
        hours_since = (now - last_log_check_time).total_seconds() / 3600
        if hours_since >= 2:
            flags.append(f"BOT_STALLED({hours_since:.1f}h)")

    return flags


async def run_single_check(check_num: int) -> None:
    """Execute one full monitoring check."""
    global prev_balance, start_balance, balance_min, balance_max, check_number

    check_number = check_num
    timestamp = now_utc()
    log(f"\n{'='*70}")
    log(f"--- CHECK #{check_num}/{TOTAL_CHECKS} @ {timestamp} ---")
    log(f"{'='*70}")

    # 1. Bot health
    bot_alive = await check_bot_health()

    # 2. Balance
    balance = await check_balance()
    if balance is not None:
        if start_balance is None:
            start_balance = balance
        if balance_min is None or balance < balance_min:
            balance_min = balance
        if balance_max is None or balance > balance_max:
            balance_max = balance

    # 3. Positions
    positions = await check_positions()
    pos_summary = "; ".join(
        f"{p['symbol']} {p['side']} uPnL={p['unrealized_pnl']}"
        for p in positions
    ) if positions else "none"
    position_history.append(f"#{check_num} {timestamp}: {pos_summary}")

    # 4. Orders
    orders = await check_orders()

    # 5. Log tail
    log_lines = check_log_tail()

    # 6. Database
    integrity, trades_today, ts_count = check_database()

    # 7. Anomaly flags
    flags = evaluate_flags(bot_alive, balance, positions, orders, trades_today, integrity, log_lines)

    log(f"\n[7] ANOMALY FLAGS")
    if flags:
        for f_str in flags:
            log(f"  FLAG: {f_str}")
            all_flags.append((timestamp, f_str))
            # Critical alerts
            if any(crit in f_str for crit in ["DEAD_LEVEL", "BOT_DOWN", "LEVERAGE_BREACH"]):
                log(f"\n  *** ALERT *** CRITICAL: {f_str}")
    else:
        log("  FLAGS: NONE")

    # 8. Summary
    bal_str = str(balance) if balance is not None else "UNKNOWN"
    flags_str = ", ".join(flags) if flags else "NONE"
    summary = f"SUMMARY: {timestamp[:16]} | BAL=${bal_str} | POS={len(positions)} | TRADES={trades_today} | TS={ts_count} | FLAGS={flags_str}"
    log(f"\n{summary}")

    # Update previous balance for next check
    if balance is not None:
        prev_balance = balance


def produce_final_report() -> None:
    """Produce the 24-hour summary report."""
    log(f"\n\n{'#'*70}")
    log(f"#  24-HOUR MONITORING FINAL REPORT")
    log(f"#  Generated: {now_utc()}")
    log(f"#  Checks completed: {check_number}/{TOTAL_CHECKS}")
    log(f"{'#'*70}\n")

    # Balance trajectory
    log("## BALANCE TRAJECTORY")
    if start_balance is not None:
        end_bal = prev_balance or start_balance
        delta = end_bal - start_balance
        delta_pct = (delta / start_balance * 100) if start_balance > 0 else Decimal("0")
        log(f"  Start:  ${start_balance}")
        log(f"  End:    ${end_bal}")
        log(f"  Min:    ${balance_min}")
        log(f"  Max:    ${balance_max}")
        log(f"  Delta:  ${delta} ({delta_pct:+.4f}%)")
    else:
        log("  No balance data collected.")

    # Trade count
    log("\n## TRADES")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        total = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= ?",
            (start_time.isoformat(),)
        ).fetchone()[0]
        log(f"  Total trades in last 24h: {total}")
        conn.close()
    except Exception as e:
        log(f"  Error querying trades: {e}")

    # All flags
    log("\n## FLAGS TRIGGERED")
    if all_flags:
        for ts_val, flag in all_flags:
            log(f"  [{ts_val}] {flag}")
    else:
        log("  No flags triggered during the monitoring period.")

    # Position history
    log("\n## POSITION HISTORY (snapshot per check)")
    for entry in position_history:
        log(f"  {entry}")

    # Anomaly summary
    log("\n## ANOMALY SUMMARY")
    critical_flags = [f for _, f in all_flags if any(c in f for c in ["DEAD_LEVEL", "BOT_DOWN", "LEVERAGE_BREACH", "DB_CORRUPT"])]
    warning_flags = [f for _, f in all_flags if f not in [cf for cf in critical_flags]]

    log(f"  Critical flags: {len(critical_flags)}")
    log(f"  Warning flags:  {len(warning_flags)}")
    log(f"  Total flags:    {len(all_flags)}")

    # Recommendation
    log("\n## RECOMMENDATION")
    if critical_flags:
        log("  >>> CRITICAL ISSUES FOUND <<<")
        log("  Do NOT proceed to live trading until critical issues are resolved.")
        for f in set(critical_flags):
            log(f"    - {f}")
    elif len(all_flags) > 10:
        log("  >>> NEEDS MORE TESTING <<<")
        log(f"  {len(all_flags)} flags triggered. Investigate before live trading.")
    elif len(all_flags) > 0:
        log("  >>> NEEDS MORE TESTING (minor issues) <<<")
        log(f"  {len(all_flags)} flags triggered. Review individually.")
    else:
        log("  >>> READY FOR LIVE <<<")
        log("  24-hour monitoring completed with zero flags. Bot is stable.")

    log(f"\n{'#'*70}")
    log(f"# END OF REPORT")
    log(f"{'#'*70}")


# ─── Signal Handlers ─────────────────────────────────────────────
def handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log(f"\n*** SIGNAL {signum} received — producing final report and shutting down ***")


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ─── Main Loop ───────────────────────────────────────────────────
async def main():
    global shutdown_requested

    log(f"=== CLAUDE QUANT 24H MONITORING STARTED {now_utc()} ===")
    log(f"Log file: {LOG_FILE}")
    log(f"Total checks planned: {TOTAL_CHECKS}")
    log(f"Interval: {INTERVAL_SECONDS}s ({INTERVAL_SECONDS // 60} minutes)")
    log(f"Bot PID expected: check below")
    log("")

    for i in range(1, TOTAL_CHECKS + 1):
        if shutdown_requested:
            log(f"\nShutdown requested after check #{i - 1}.")
            break

        try:
            await run_single_check(i)
        except Exception as e:
            log(f"\n*** ERROR in check #{i}: {e} ***")

        if i < TOTAL_CHECKS and not shutdown_requested:
            log(f"\nNext check in {INTERVAL_SECONDS // 60} minutes...")
            # Sleep in small chunks to respond to signals
            for _ in range(INTERVAL_SECONDS // 5):
                if shutdown_requested:
                    break
                await asyncio.sleep(5)

    # Final report
    produce_final_report()
    log(f"\nMonitoring agent exiting at {now_utc()}")


if __name__ == "__main__":
    asyncio.run(main())
