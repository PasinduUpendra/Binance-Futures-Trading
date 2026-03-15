#!/usr/bin/env python3
"""
Watchdog Tools — Structured data extraction for the Watchdog Claude Agent.

Provides CLI subcommands that return JSON, designed to be called by the
watchdog agent via Bash tool:

    .venv/bin/python scripts/watchdog_tools.py health
    .venv/bin/python scripts/watchdog_tools.py logs --hours 2
    .venv/bin/python scripts/watchdog_tools.py performance
    .venv/bin/python scripts/watchdog_tools.py mistakes
    .venv/bin/python scripts/watchdog_tools.py market
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_FILE = PROJECT_ROOT / "user_data" / "logs" / "bot.log"
PID_FILE = PROJECT_ROOT / "user_data" / "logs" / "bot.pid"
STATE_DIR = PROJECT_ROOT / "user_data" / "agent_state"
WATCHDOG_LOG = PROJECT_ROOT / "user_data" / "logs" / "watchdog.log"


def _json_out(data: dict | list) -> None:
    """Print JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# ─── HEALTH ───────────────────────────────────────────────────────────────────


def cmd_health(_args: argparse.Namespace) -> None:
    """Check if bot process is alive and responsive."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot_running": False,
        "pid": None,
        "uptime_hours": None,
        "last_log_line": None,
        "last_log_age_seconds": None,
        "cycles_detected": 0,
        "last_cycle_number": None,
        "issues": [],
    }

    # Check PID
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            result["pid"] = pid
            os.kill(pid, 0)  # Check if process exists
            result["bot_running"] = True
        except (ValueError, ProcessLookupError):
            result["issues"].append(f"PID file exists but process {result['pid']} not found — BOT CRASHED")
        except PermissionError:
            result["bot_running"] = True  # Process exists but we can't signal it
    else:
        result["issues"].append("No PID file found — bot may not have started")

    # Check last log activity
    if LOG_FILE.exists():
        stat = LOG_FILE.stat()
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        result["last_log_age_seconds"] = int(age.total_seconds())

        # Read last few lines
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read_size = min(size, 4096)
                f.seek(size - read_size)
                tail = f.read().decode("utf-8", errors="replace")
                lines = tail.strip().split("\n")
                result["last_log_line"] = lines[-1][:200] if lines else None

                # Count cycles in last chunk
                for line in lines:
                    m = re.search(r"=== Cycle (\d+) starting ===", line)
                    if m:
                        result["last_cycle_number"] = int(m.group(1))
                        result["cycles_detected"] += 1
        except Exception as e:
            result["issues"].append(f"Error reading log: {e}")

        if age.total_seconds() > 7200:  # 2 hours
            result["issues"].append(f"Log file not updated in {age.total_seconds()/3600:.1f}h — bot may be stuck")
    else:
        result["issues"].append("bot.log not found — bot never started or log path wrong")

    _json_out(result)


# ─── LOGS ─────────────────────────────────────────────────────────────────────

# Regex patterns for log parsing
RE_CYCLE_START = re.compile(r"=== Cycle (\d+) starting ===")
RE_CYCLE_END = re.compile(r"=== Cycle (\d+) complete \(([\d.]+)s\) ===")
RE_REGIME = re.compile(r"Regime detected: (\w+) \(confidence=([\d.]+)%, ADX=([\d.]+)")
RE_REGIME_ROUTE = re.compile(r"Regime (\w+).*-> (SupertrendTrend|MeanReversion|BreakoutTrader|TrendFollower|NO TRADE)")
RE_SIGNAL_NONE = re.compile(r"Strategy (\w+) returned NONE signal: (.+)")
RE_SIGNAL_APPROVED = re.compile(
    r"Adaptive signal APPROVED: (\w+) (\w+) @ ([\d.]+) "
    r"\(confidence=([\d.]+)%, regime=(\w+), strategy=(\w+)\)"
)
RE_TRADE_PLACED = re.compile(r"TRADE PLACED: (\S+) (\w+) @ ([\d.]+) x(\d+)")
RE_NO_SIGNALS = re.compile(r"No valid signals this cycle")
RE_CB_LEVEL = re.compile(r"Circuit breaker (\w+) .* balance \$([\d.]+)")
RE_ERROR = re.compile(r"ERROR|CRITICAL|HALT")
RE_POSITION_COUNT = re.compile(r"GET_OPEN_POSITIONS_OK count=(\d+)")
RE_BALANCE = re.compile(r"balance.*\$([\d.]+)")
RE_DISABLED = re.compile(r"NO TRADE \((\w+) disabled")
RE_NO_FLIP = re.compile(r"No flip.*prev_dir=([-\d.]+), cur_dir=([-\d.]+)")
RE_TP_PLACED = re.compile(r"Take-profit.*placed")
RE_SL_PLACED = re.compile(r"Stop-loss.*placed|STOP_MARKET.*placed")
RE_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def cmd_logs(args: argparse.Namespace) -> None:
    """Parse recent bot.log and return structured summary."""
    hours = args.hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    result = {
        "period_hours": hours,
        "cutoff": cutoff.isoformat(),
        "cycles": [],
        "signals_approved": [],
        "trades_placed": [],
        "rejections": {},
        "errors": [],
        "regimes": {},
        "disabled_strategy_hits": {},
        "no_flip_count": 0,
        "balance_snapshots": [],
        "tp_orders_placed": 0,
        "sl_orders_placed": 0,
    }

    if not LOG_FILE.exists():
        result["error"] = "bot.log not found"
        _json_out(result)
        return

    current_cycle = None

    with open(LOG_FILE, "r") as f:
        for line in f:
            # Filter by time
            ts_match = RE_TIMESTAMP.match(line)
            if ts_match:
                try:
                    line_time = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                    line_time = line_time.replace(tzinfo=timezone.utc)
                    if line_time < cutoff:
                        continue
                except ValueError:
                    pass

            # Cycle start
            m = RE_CYCLE_START.search(line)
            if m:
                current_cycle = {"number": int(m.group(1)), "duration_s": None, "signals": 0, "trades": 0}
                continue

            # Cycle end
            m = RE_CYCLE_END.search(line)
            if m:
                if current_cycle and current_cycle["number"] == int(m.group(1)):
                    current_cycle["duration_s"] = float(m.group(2))
                    result["cycles"].append(current_cycle)
                current_cycle = None
                continue

            # Regime
            m = RE_REGIME.search(line)
            if m:
                # Extract pair from surrounding context (best-effort)
                regime_info = {
                    "regime": m.group(1),
                    "confidence": float(m.group(2)),
                    "adx": float(m.group(3)),
                }
                # Use regime as key, count occurrences
                key = m.group(1)
                result["regimes"][key] = result["regimes"].get(key, 0) + 1
                continue

            # Disabled strategy hit
            m = RE_DISABLED.search(line)
            if m:
                strat = m.group(1)
                result["disabled_strategy_hits"][strat] = result["disabled_strategy_hits"].get(strat, 0) + 1
                continue

            # No flip
            if RE_NO_FLIP.search(line):
                result["no_flip_count"] += 1
                continue

            # Signal approved
            m = RE_SIGNAL_APPROVED.search(line)
            if m:
                result["signals_approved"].append({
                    "direction": m.group(1),
                    "pair": m.group(2),
                    "price": float(m.group(3)),
                    "confidence": float(m.group(4)),
                    "regime": m.group(5),
                    "strategy": m.group(6),
                })
                if current_cycle:
                    current_cycle["signals"] += 1
                continue

            # Trade placed
            m = RE_TRADE_PLACED.search(line)
            if m:
                result["trades_placed"].append({
                    "symbol": m.group(1),
                    "direction": m.group(2),
                    "price": float(m.group(3)),
                    "leverage": int(m.group(4)),
                })
                if current_cycle:
                    current_cycle["trades"] += 1
                continue

            # Signal rejection
            m = RE_SIGNAL_NONE.search(line)
            if m:
                reason = m.group(2)[:60]
                key = f"{m.group(1)}: {reason}"
                result["rejections"][key] = result["rejections"].get(key, 0) + 1
                continue

            # TP/SL placed
            if RE_TP_PLACED.search(line):
                result["tp_orders_placed"] += 1
            if RE_SL_PLACED.search(line):
                result["sl_orders_placed"] += 1

            # Balance
            m = RE_CB_LEVEL.search(line)
            if m:
                result["balance_snapshots"].append({
                    "level": m.group(1),
                    "balance": float(m.group(2)),
                })
                continue

            # Errors
            if RE_ERROR.search(line) and "ERROR" in line:
                result["errors"].append(line.strip()[-200:])
                continue

    _json_out(result)


# ─── PERFORMANCE ──────────────────────────────────────────────────────────────


def cmd_performance(_args: argparse.Namespace) -> None:
    """Calculate current performance metrics."""
    from dotenv import load_dotenv
    load_dotenv()

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": None,
        "initial_balance": 5000.0,  # Testnet default
        "total_return_pct": None,
        "daily_compound_rate": None,
        "target_daily_rate": 1.0,
        "gap_to_target": None,
        "trades_total": 0,
        "trades_won": 0,
        "trades_lost": 0,
        "win_rate": None,
        "hours_running": 0,
        "days_running": 0,
        "error": None,
    }

    # Try to get balance from state files
    state_file = STATE_DIR / "watchdog_state.json"
    cycle_state = STATE_DIR / "last_cycle.json"

    if cycle_state.exists():
        try:
            data = json.loads(cycle_state.read_text())
            if "balance" in data:
                result["balance"] = float(data["balance"])
        except (json.JSONDecodeError, KeyError):
            pass

    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            result["trades_total"] = data.get("trades_total", 0)
            result["trades_won"] = data.get("trades_won", 0)
            result["trades_lost"] = data.get("trades_lost", 0)
            if data.get("current_balance"):
                result["balance"] = float(data["current_balance"])
            if data.get("initial_balance"):
                result["initial_balance"] = float(data["initial_balance"])
        except (json.JSONDecodeError, KeyError):
            pass

    # Try to get balance from bot.log (last CB check)
    if result["balance"] is None and LOG_FILE.exists():
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read_size = min(size, 32768)
                f.seek(size - read_size)
                tail = f.read().decode("utf-8", errors="replace")
                for line in reversed(tail.split("\n")):
                    m = RE_CB_LEVEL.search(line)
                    if m:
                        result["balance"] = float(m.group(2))
                        break
        except Exception:
            pass

    # Calculate metrics
    if result["balance"] is not None and result["initial_balance"] > 0:
        ret = (result["balance"] - result["initial_balance"]) / result["initial_balance"]
        result["total_return_pct"] = round(ret * 100, 4)

        # Estimate days running from log
        if LOG_FILE.exists():
            try:
                first_line = ""
                with open(LOG_FILE, "r") as f:
                    first_line = f.readline()
                m = RE_TIMESTAMP.match(first_line)
                if m:
                    start_time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    start_time = start_time.replace(tzinfo=timezone.utc)
                    elapsed = datetime.now(timezone.utc) - start_time
                    result["hours_running"] = round(elapsed.total_seconds() / 3600, 2)
                    result["days_running"] = round(elapsed.total_seconds() / 86400, 4)
            except Exception:
                pass

        if result["days_running"] >= 1:
            daily_rate = ((1 + ret) ** (1 / result["days_running"]) - 1) * 100
            result["daily_compound_rate"] = round(daily_rate, 4)
            result["gap_to_target"] = round(1.0 - daily_rate, 4)

    if result["trades_total"] > 0:
        result["win_rate"] = round(result["trades_won"] / result["trades_total"] * 100, 2)

    _json_out(result)


# ─── MISTAKES ─────────────────────────────────────────────────────────────────


def cmd_mistakes(_args: argparse.Namespace) -> None:
    """Run automated mistake detection on recent bot activity."""
    mistakes = []

    # 1. Check if bot is running
    bot_alive = False
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            bot_alive = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    if not bot_alive:
        mistakes.append({
            "severity": "CRITICAL",
            "category": "BOT_CRASH",
            "message": "Bot process not running",
            "fix": "Restart with: .venv/bin/python scripts/run_bot.py",
        })

    # 2. Check log staleness
    if LOG_FILE.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            LOG_FILE.stat().st_mtime, tz=timezone.utc
        )
        if age.total_seconds() > 7200:
            mistakes.append({
                "severity": "CRITICAL",
                "category": "BOT_STUCK",
                "message": f"No log output for {age.total_seconds()/3600:.1f} hours",
                "fix": "Check if bot process is hung. Kill and restart.",
            })

    # 3. Parse last 24h of logs for patterns
    if LOG_FILE.exists():
        cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        cycle_durations = []
        error_count = 0
        trade_count = 0
        signal_count = 0
        cycle_count = 0
        tp_count = 0
        sl_count = 0

        with open(LOG_FILE, "r") as f:
            for line in f:
                ts_match = RE_TIMESTAMP.match(line)
                if ts_match:
                    try:
                        line_time = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                        line_time = line_time.replace(tzinfo=timezone.utc)
                        if line_time < cutoff_24h:
                            continue
                    except ValueError:
                        pass

                m = RE_CYCLE_END.search(line)
                if m:
                    cycle_count += 1
                    cycle_durations.append(float(m.group(2)))

                if RE_SIGNAL_APPROVED.search(line):
                    signal_count += 1
                if RE_TRADE_PLACED.search(line):
                    trade_count += 1
                if RE_TP_PLACED.search(line):
                    tp_count += 1
                if RE_SL_PLACED.search(line):
                    sl_count += 1
                if "ERROR" in line:
                    error_count += 1

        # Slow cycles
        slow_cycles = [d for d in cycle_durations if d > 30]
        if slow_cycles:
            mistakes.append({
                "severity": "WARNING",
                "category": "SLOW_CYCLES",
                "message": f"{len(slow_cycles)} cycles took >30s (max: {max(slow_cycles):.1f}s)",
                "fix": "Check API latency. Consider reducing indicator calculations.",
            })

        # Missing TP orders
        if trade_count > 0 and tp_count < trade_count:
            mistakes.append({
                "severity": "CRITICAL",
                "category": "MISSING_TP",
                "message": f"{trade_count} trades placed but only {tp_count} TP orders",
                "fix": "Check place_take_profit() in orchestrator. TP must be placed for every trade.",
            })

        # Missing SL orders
        if trade_count > 0 and sl_count < trade_count:
            mistakes.append({
                "severity": "CRITICAL",
                "category": "MISSING_SL",
                "message": f"{trade_count} trades placed but only {sl_count} SL orders",
                "fix": "Check place_stop_loss() in orchestrator. SL must be placed for every trade.",
            })

        # High error rate
        if error_count > 10:
            mistakes.append({
                "severity": "WARNING",
                "category": "HIGH_ERROR_RATE",
                "message": f"{error_count} errors in last 24h",
                "fix": "Review error logs: grep ERROR user_data/logs/bot.log | tail -20",
            })

        # No trades for extended period
        if cycle_count >= 24 and trade_count == 0:
            mistakes.append({
                "severity": "WARNING",
                "category": "NO_TRADES",
                "message": f"0 trades in {cycle_count} cycles (24h+). SupertrendTrend waiting for flip.",
                "fix": "This may be normal — v4 backtest averages 1 trade per 4.4 days. Adding pairs is a research direction but must pass strategy versioning pipeline.",
            })

    _json_out(mistakes)


# ─── MARKET ───────────────────────────────────────────────────────────────────


def cmd_market(_args: argparse.Namespace) -> None:
    """Fetch current market state for all trading pairs."""
    from dotenv import load_dotenv
    load_dotenv()

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pairs": {},
        "error": None,
    }

    try:
        from src.data.market_data import MarketDataClient
        from src.data.indicator_engine import IndicatorEngine
        from src.strategies.regime_detector import RegimeDetector
        import pandas as pd

        async def _fetch():
            client = MarketDataClient()
            await client.connect()
            indicator_engine = IndicatorEngine()
            regime_detector = RegimeDetector()

            pairs = ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"]

            for pair in pairs:
                try:
                    # Fetch 4H data
                    raw_4h = await client.fetch_ohlcv(pair, "4h", limit=200)
                    df_4h = pd.DataFrame(raw_4h)
                    df_4h = indicator_engine.calculate_all(df_4h)

                    # Detect regime
                    regime = regime_detector.detect(df_4h)

                    # Get supertrend info
                    last_st_dir = float(df_4h["supertrend_direction"].iloc[-1])
                    prev_st_dir = float(df_4h["supertrend_direction"].iloc[-2])
                    flip = last_st_dir != prev_st_dir

                    result["pairs"][pair] = {
                        "regime": regime.regime.value,
                        "regime_confidence": regime.confidence,
                        "adx": regime.adx,
                        "supertrend_direction": "BULLISH" if last_st_dir == 1.0 else "BEARISH",
                        "supertrend_flipped": flip,
                        "would_trade": regime.regime.value == "TRENDING" and regime.adx >= 18 and flip,
                        "close": float(df_4h["close"].iloc[-1]),
                        "rsi": float(df_4h["rsi"].iloc[-1]),
                        "atr": float(df_4h["atr"].iloc[-1]),
                    }
                except Exception as e:
                    result["pairs"][pair] = {"error": str(e)}

            await client.close()

        asyncio.run(_fetch())

    except Exception as e:
        result["error"] = str(e)

    _json_out(result)


# ─── REPORT (full formatted report) ──────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> None:
    """Generate and save a formatted watchdog report."""
    # Collect all data
    import io
    from contextlib import redirect_stdout

    sections = {}

    # Health
    f = io.StringIO()
    with redirect_stdout(f):
        cmd_health(args)
    sections["health"] = json.loads(f.getvalue())

    # Logs (last 4 hours)
    f = io.StringIO()
    args_logs = argparse.Namespace(hours=4)
    with redirect_stdout(f):
        cmd_logs(args_logs)
    sections["logs"] = json.loads(f.getvalue())

    # Mistakes
    f = io.StringIO()
    with redirect_stdout(f):
        cmd_mistakes(args)
    sections["mistakes"] = json.loads(f.getvalue())

    # Build report
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    h = sections["health"]
    l = sections["logs"]
    m = sections["mistakes"]

    report_lines = [
        f"=== WATCHDOG REPORT — {now} ===",
        "",
        f"BOT HEALTH: {'RUNNING' if h['bot_running'] else 'DOWN'} (PID {h['pid']})",
        f"LAST LOG: {h['last_log_age_seconds']}s ago",
        f"CYCLES (last 4h): {len(l['cycles'])}",
        "",
        f"TRADES (last 4h): {len(l['trades_placed'])}",
        f"SIGNALS (last 4h): {len(l['signals_approved'])}",
        f"ERRORS (last 4h): {len(l['errors'])}",
        "",
        "REGIMES (last 4h):",
    ]
    for regime, count in l.get("regimes", {}).items():
        report_lines.append(f"  {regime}: {count} occurrences")

    if l.get("disabled_strategy_hits"):
        report_lines.append("")
        report_lines.append("DISABLED STRATEGY HITS:")
        for strat, count in l["disabled_strategy_hits"].items():
            report_lines.append(f"  {strat}: {count} times → NO TRADE")

    report_lines.append(f"\nNO-FLIP COUNT: {l['no_flip_count']} (SupertrendTrend waiting)")

    if m:
        report_lines.append(f"\nMISTAKES DETECTED: {len(m)}")
        for mistake in m:
            report_lines.append(f"  [{mistake['severity']}] {mistake['category']}: {mistake['message']}")
            report_lines.append(f"    FIX: {mistake['fix']}")
    else:
        report_lines.append("\nMISTAKES DETECTED: 0")

    report_lines.append("\n===")

    report_text = "\n".join(report_lines)
    print(report_text)

    # Also save to watchdog log
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_LOG, "a") as f:
        f.write(f"\n{report_text}\n")


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Watchdog tools for Claude Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Check bot health")

    logs_parser = sub.add_parser("logs", help="Parse recent bot logs")
    logs_parser.add_argument("--hours", type=float, default=1, help="Hours of logs to parse")

    sub.add_parser("performance", help="Get performance metrics")
    sub.add_parser("mistakes", help="Detect mistakes")
    sub.add_parser("market", help="Fetch current market state")
    sub.add_parser("report", help="Generate full formatted report")

    args = parser.parse_args()

    commands = {
        "health": cmd_health,
        "logs": cmd_logs,
        "performance": cmd_performance,
        "mistakes": cmd_mistakes,
        "market": cmd_market,
        "report": cmd_report,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
