#!/usr/bin/env python3
"""Claude Quant Live Monitoring — checks bot health every 5 minutes.

Runs for 12 hours. Logs to console and user_data/logs/monitor.log.
Checks:
  1. Bot process is alive
  2. Binance balance vs INITIAL_CAPITAL
  3. Open positions and their PnL
  4. Recent log entries for errors
  5. Circuit breaker level
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "user_data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Logger setup
logger = logging.getLogger("monitor")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s [MONITOR] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

fh = logging.FileHandler(LOG_DIR / "monitor.log", encoding="utf-8")
fh.setLevel(logging.INFO)
fh.setFormatter(fmt)
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
logger.addHandler(ch)

INTERVAL_SECONDS = 300  # 5 minutes
DURATION_HOURS = 12
HARD_FLOOR = Decimal("30.0")
INITIAL_CAPITAL = Decimal(os.getenv("INITIAL_CAPITAL", "68.33"))


def _find_bot_pid() -> int | None:
    """Find the run_bot.py process."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_bot.py"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split("\n")
        if pids and pids[0]:
            return int(pids[0])
    except Exception:
        pass
    return None


async def _fetch_balance_and_positions() -> tuple[Decimal, list[dict]]:
    """Fetch balance and open positions from Binance."""
    import ccxt.async_support as ccxt_async

    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    if testnet:
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
    else:
        api_key = os.getenv("BINANCE_API_KEY_PROD", "")
        api_secret = os.getenv("BINANCE_API_SECRET_PROD", "")

    exchange = ccxt_async.binanceusdm({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
    })
    if testnet:
        exchange.enable_demo_trading(True)

    try:
        balance = await exchange.fetch_balance()
        usdt_total = Decimal(str(balance.get("USDT", {}).get("total", 0)))

        positions = await exchange.fetch_positions()
        open_pos = []
        for p in positions:
            contracts = float(p.get("contracts", 0))
            if contracts > 0:
                open_pos.append({
                    "symbol": p.get("symbol", "?"),
                    "side": p.get("side", "?"),
                    "contracts": contracts,
                    "notional": float(p.get("notional", 0)),
                    "pnl": float(p.get("unrealizedPnl", 0)),
                    "leverage": p.get("leverage", "?"),
                    "liq_price": p.get("liquidationPrice", "?"),
                })
        return usdt_total, open_pos
    finally:
        await exchange.close()


def _check_recent_errors(n: int = 20) -> list[str]:
    """Scan last N lines of bot.log for ERROR/CRITICAL entries."""
    log_path = LOG_DIR / "bot.log"
    if not log_path.exists():
        return ["bot.log not found"]
    try:
        result = subprocess.run(
            ["tail", "-n", str(n), str(log_path)],
            capture_output=True, text=True, timeout=5,
        )
        errors = []
        for line in result.stdout.strip().split("\n"):
            if "ERROR" in line or "CRITICAL" in line:
                errors.append(line.strip())
        return errors
    except Exception as e:
        return [f"Log read error: {e}"]


async def run_check(check_num: int) -> None:
    """Execute a single monitoring check."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info("=" * 60)
    logger.info("CHECK #%d — %s", check_num, now)

    # 1. Bot process
    pid = _find_bot_pid()
    if pid:
        logger.info("Bot process: ALIVE (PID %d)", pid)
    else:
        logger.error("Bot process: NOT RUNNING!")

    # 2. Balance and positions
    try:
        balance, positions = await _fetch_balance_and_positions()
        pnl_pct = ((balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100) if INITIAL_CAPITAL > 0 else Decimal("0")

        logger.info("Balance: $%.4f USDT (%.2f%% from initial $%.2f)", balance, pnl_pct, INITIAL_CAPITAL)

        if balance < HARD_FLOOR:
            logger.critical("BALANCE BELOW HARD FLOOR ($30)! Balance: $%.4f", balance)
        elif balance < Decimal("45"):
            logger.warning("Balance in YELLOW zone: $%.4f", balance)

        # 3. Open positions
        if positions:
            logger.info("Open positions: %d", len(positions))
            for p in positions:
                logger.info(
                    "  %s %s | %.4f contracts | notional $%.2f | PnL $%.4f | lev %sx | liq %s",
                    p["symbol"], p["side"], p["contracts"],
                    p["notional"], p["pnl"], p["leverage"], p["liq_price"],
                )
        else:
            logger.info("Open positions: 0")

    except Exception as e:
        logger.error("Failed to fetch balance/positions: %s: %s", type(e).__name__, e)

    # 4. Recent log errors
    errors = _check_recent_errors(50)
    if errors:
        logger.warning("Recent errors in bot.log: %d", len(errors))
        for err in errors[-3:]:  # Show last 3
            logger.warning("  >> %s", err[:200])
    else:
        logger.info("No recent errors in bot.log")

    logger.info("-" * 60)


async def main() -> None:
    total_checks = (DURATION_HOURS * 3600) // INTERVAL_SECONDS
    mode = "TESTNET" if os.getenv("BINANCE_TESTNET", "false").lower() == "true" else "MAINNET"

    logger.info("=" * 60)
    logger.info("Claude Quant Monitor — Starting")
    logger.info("Mode: %s | Interval: %ds | Duration: %dh | Total checks: %d",
                mode, INTERVAL_SECONDS, DURATION_HOURS, total_checks)
    logger.info("Initial capital: $%.8f | Hard floor: $%.2f", INITIAL_CAPITAL, HARD_FLOOR)
    logger.info("=" * 60)

    for i in range(1, total_checks + 1):
        await run_check(i)
        if i < total_checks:
            await asyncio.sleep(INTERVAL_SECONDS)

    logger.info("Monitoring complete after %d hours.", DURATION_HOURS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user.")
