#!/usr/bin/env python3
"""24H Monitoring check-in script for Claude Quant.

Read-only: NEVER modifies code, config, database, or state files.
Run: .venv/bin/python scripts/monitor_check.py <log_file> <check_number> [prev_balance]
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()


async def run_check(log_file: str, check_num: int, prev_balance: float | None = None) -> None:
    results: list[str] = []
    flags: list[str] = []
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results.append(f"\n--- CHECK #{check_num} {now_utc} ---\n")

    # 1. BOT HEALTH
    results.append("=== 1. BOT HEALTH ===")
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    bot_lines = [l for l in ps.stdout.split("\n") if "run_bot.py" in l and "grep" not in l]
    if bot_lines:
        pid = bot_lines[0].split()[1]
        cpu = bot_lines[0].split()[2]
        mem = bot_lines[0].split()[3]
        results.append(f"BOT RUNNING: PID={pid} CPU={cpu}% MEM={mem}%")
    else:
        results.append("*** ALERT *** FLAG: BOT_DOWN")
        flags.append("BOT_DOWN")

    # 2. BALANCE
    results.append("\n=== 2. BALANCE ===")
    balance_val: float | None = None
    try:
        from src.data.market_data import MarketDataClient
        client = MarketDataClient()
        await client.connect()
        bal = await client.get_margin_balance()
        results.append(f"BALANCE={bal}")
        balance_val = float(bal)
        if balance_val < 30:
            results.append("*** ALERT *** FLAG: DEAD_LEVEL (Rule #1 violation)")
            flags.append("DEAD_LEVEL")
        if prev_balance is not None and prev_balance > 0:
            pct_change = ((balance_val - prev_balance) / prev_balance) * 100
            results.append(f"CHANGE_FROM_LAST: {pct_change:+.3f}%")
            if pct_change < -5.0:
                results.append(f"*** ALERT *** FLAG: RAPID_DRAWDOWN ({pct_change:.2f}% since last check)")
                flags.append("RAPID_DRAWDOWN")
        await client.close()
    except Exception as e:
        results.append(f"BALANCE_ERROR: {e}")

    # 3. OPEN POSITIONS
    results.append("\n=== 3. OPEN POSITIONS ===")
    pos_count = 0
    position_symbols: set[str] = set()
    try:
        from src.execution.position_tracker import PositionTracker
        tracker = PositionTracker()
        await tracker.connect()
        positions = await tracker.get_open_positions()
        for p in positions:
            results.append(
                f"POS: {p.symbol} {p.side} size={p.size} entry={p.entry_price} "
                f"upnl={p.unrealized_pnl} lev={p.leverage}"
            )
            pos_count += 1
            position_symbols.add(p.symbol)
            if float(p.leverage) > 10:
                results.append(f"*** ALERT *** FLAG: LEVERAGE_BREACH on {p.symbol} lev={p.leverage}")
                flags.append("LEVERAGE_BREACH")
        if not positions:
            results.append("NO_OPEN_POSITIONS")
        if pos_count > 3:
            results.append(f"FLAG: OVER_POSITIONED count={pos_count}")
            flags.append("OVER_POSITIONED")
        await tracker.close()
    except Exception as e:
        results.append(f"POSITION_ERROR: {e}")

    # 4. OPEN ORDERS (orphan detection)
    results.append("\n=== 4. OPEN ORDERS ===")
    order_symbols: set[str] = set()
    try:
        from src.execution.order_manager import OrderManager
        om = OrderManager()
        await om.connect()
        pairs = [
            "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
            "DOGE/USDT:USDT", "XRP/USDT:USDT", "LINK/USDT:USDT",
            "AVAX/USDT:USDT", "SUI/USDT:USDT", "ADA/USDT:USDT",
        ]
        for pair in pairs:
            orders = await om.get_open_orders(pair)
            for o in orders:
                results.append(
                    f"ORDER: {pair} {o.order_type} {o.side} amt={o.amount} "
                    f"price={o.stop_price or o.price}"
                )
                order_symbols.add(pair)
        if not order_symbols:
            results.append("NO_OPEN_ORDERS")
        # Orphan detection
        orphans = order_symbols - position_symbols
        if orphans:
            results.append(f"FLAG: ORPHAN_ORDERS for {orphans}")
            flags.append("ORPHAN_ORDERS")
        await om.close()
    except Exception as e:
        results.append(f"ORDER_ERROR: {e}")

    # 5. LOG TAIL (last 30 lines)
    results.append("\n=== 5. RECENT LOGS (last 30 lines) ===")
    log_path = PROJECT_ROOT / "user_data" / "logs" / "bot.log"
    if log_path.exists():
        lines = log_path.read_text().strip().split("\n")
        for line in lines[-30:]:
            results.append(line)
    else:
        nohup_path = PROJECT_ROOT / "nohup.out"
        if nohup_path.exists():
            lines = nohup_path.read_text().strip().split("\n")
            for line in lines[-30:]:
                results.append(line)
        else:
            results.append("NO_LOG_FILE")

    # Check for bot stall (no new log lines in 2+ hours)
    if log_path.exists():
        import re
        lines = log_path.read_text().strip().split("\n")
        if lines:
            last_line = lines[-1]
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", last_line)
            if ts_match:
                last_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                last_ts = last_ts.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
                if age_hours > 2.0:
                    results.append(f"FLAG: BOT_STALLED (last log {age_hours:.1f}h ago)")
                    flags.append("BOT_STALLED")

    # 6. DATABASE INTEGRITY
    results.append("\n=== 6. DATABASE ===")
    trades_today = 0
    db_path = PROJECT_ROOT / "user_data" / "claude_quant.db"
    try:
        conn = sqlite3.connect(str(db_path))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        results.append(f"INTEGRITY: {integrity}")
        if integrity != "ok":
            results.append("FLAG: DB_CORRUPT")
            flags.append("DB_CORRUPT")
        trades_today = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE date(timestamp)=date('now')"
        ).fetchone()[0]
        results.append(f"TRADES_TODAY={trades_today}")
        if trades_today > 20:
            results.append("FLAG: OVERTRADE_BREACH")
            flags.append("OVERTRADE_BREACH")
        ts_count = conn.execute("SELECT COUNT(*) FROM trailing_stops").fetchone()[0]
        results.append(f"TRAILING_STOPS_ACTIVE={ts_count}")
        conn.close()
    except Exception as e:
        results.append(f"DB_ERROR: {e}")

    # 7. SUMMARY
    flag_str = ", ".join(flags) if flags else "NONE"
    bal_str = f"${balance_val:.2f}" if balance_val is not None else "N/A"
    summary = (
        f"SUMMARY: {datetime.now(timezone.utc).strftime('%H:%M')} | "
        f"BAL={bal_str} | POS={pos_count} | TRADES={trades_today} | "
        f"FLAGS={flag_str}"
    )
    results.append(f"\n{'='*60}")
    results.append(summary)
    results.append(f"{'='*60}\n")

    if flags:
        for f_name in flags:
            if f_name in ("DEAD_LEVEL", "BOT_DOWN", "LEVERAGE_BREACH"):
                results.append(f"*** ALERT *** CRITICAL FLAG: {f_name}")

    # Write to log file
    with open(log_file, "a") as f:
        f.write("\n".join(results) + "\n")

    # Print to console
    print("\n".join(results))

    # Print balance for next invocation
    if balance_val is not None:
        print(f"\n__BALANCE_FOR_NEXT__={balance_val}")


if __name__ == "__main__":
    log_file = sys.argv[1] if len(sys.argv) > 1 else "user_data/monitoring_logs/audit.log"
    check_num = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    prev_bal = float(sys.argv[3]) if len(sys.argv) > 3 else None
    asyncio.run(run_check(log_file, check_num, prev_bal))
