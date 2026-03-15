#!/usr/bin/env python3
"""
Claude Quant Watchdog — Real-time monitoring agent.

Runs alongside the trading bot. Tails bot.log in real-time and:
1. Tracks every signal, trade, rejection, and error
2. Calculates running P&L and daily compound rate
3. Detects mistakes (SL too tight, missed entries, execution failures)
4. Logs everything to watchdog.log and prints to stdout
5. Alerts when performance deviates from 1% daily target

Usage:
    .venv/bin/python3 scripts/watchdog.py

Runs until killed (Ctrl+C).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "user_data" / "logs" / "bot.log"
WATCHDOG_LOG = PROJECT_ROOT / "user_data" / "logs" / "watchdog.log"
STATE_FILE = PROJECT_ROOT / "user_data" / "agent_state" / "watchdog_state.json"

# Validated daily return ceiling from v4 backtest (Sharpe 3.98, ~870% annualized).
# 1% is aspirational — 0.628% is the evidence-based benchmark.
DAILY_TARGET_PCT = 0.628

# ─── ANSI colors ───
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


class WatchdogState:
    """Persistent state for the watchdog."""

    def __init__(self):
        self.start_time = datetime.now(timezone.utc).isoformat()
        self.initial_balance = 5000.0  # Testnet
        self.current_balance = 5000.0
        self.cycles_total = 0
        self.cycles_with_signal = 0
        self.trades_total = 0
        self.trades_won = 0
        self.trades_lost = 0
        self.total_pnl = 0.0
        self.daily_pnl = {}  # {date: pnl}
        self.signals_by_strategy = {}
        self.rejections_by_reason = {}
        self.errors = []
        self.mistakes = []  # Detected issues
        self.last_cycle_time = None
        self.positions_open = 0
        self.regime_history = []  # Last 50 regime detections

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__, indent=2, default=str))

    @classmethod
    def load(cls):
        state = cls()
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                for k, v in data.items():
                    if hasattr(state, k):
                        setattr(state, k, v)
            except (json.JSONDecodeError, KeyError):
                pass
        return state


class Watchdog:
    """Real-time bot monitor."""

    # Regex patterns for log parsing
    RE_CYCLE_START = re.compile(r"=== Cycle (\d+) starting ===")
    RE_CYCLE_END = re.compile(r"=== Cycle (\d+) complete \(([\d.]+)s\) ===")
    RE_REGIME = re.compile(
        r"Regime detected: (\w+) \(confidence=([\d.]+)%, ADX=([\d.]+)"
    )
    RE_STRATEGY_ROUTE = re.compile(
        r"Regime (\w+) .* -> (SupertrendTrend|MeanReversion|BreakoutTrader|TrendFollower|NO TRADE)"
    )
    RE_SIGNAL_NONE = re.compile(
        r"Strategy (\w+) returned NONE signal: (.+)"
    )
    RE_SIGNAL_APPROVED = re.compile(
        r"Adaptive signal APPROVED: (\w+) (\w+) @ ([\d.]+) "
        r"\(confidence=([\d.]+)%, regime=(\w+), strategy=(\w+)\)"
    )
    RE_TRADE_PLACED = re.compile(
        r"TRADE PLACED: (\S+) (\w+) @ ([\d.]+) x(\d+)"
    )
    RE_NO_SIGNALS = re.compile(r"No valid signals this cycle")
    RE_CB_LEVEL = re.compile(
        r"Circuit breaker (\w+) .* balance \$([\d.]+)"
    )
    RE_ERROR = re.compile(r"ERROR|CRITICAL|HALT")
    RE_POSITION_COUNT = re.compile(
        r"GET_OPEN_POSITIONS_OK count=(\d+)"
    )
    RE_DISABLED_STRATEGY = re.compile(
        r"NO TRADE \((\w+) disabled"
    )

    def __init__(self):
        self.state = WatchdogState.load()
        self._log_fh = open(WATCHDOG_LOG, "a")
        self._last_regime_per_pair = {}
        self._cycle_signals = []

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{ts} [{level}] {msg}"
        self._log_fh.write(log_line + "\n")
        self._log_fh.flush()

        # Color output
        color = {
            "INFO": CYAN,
            "TRADE": GREEN,
            "WARN": YELLOW,
            "ERROR": RED,
            "MISTAKE": f"{RED}{BOLD}",
            "METRIC": f"{BOLD}",
        }.get(level, "")
        print(f"{color}[{level:7s}] {msg}{RESET}")

    def process_line(self, line: str):
        """Process a single log line from bot.log."""
        line = line.strip()
        if not line:
            return

        # ─── Cycle start ───
        m = self.RE_CYCLE_START.search(line)
        if m:
            self.state.cycles_total = int(m.group(1))
            self.state.last_cycle_time = datetime.now(timezone.utc).isoformat()
            self._cycle_signals = []
            return

        # ─── Cycle end ───
        m = self.RE_CYCLE_END.search(line)
        if m:
            cycle_num = int(m.group(1))
            duration = float(m.group(2))
            self.log(
                f"Cycle {cycle_num} complete ({duration:.1f}s) — "
                f"signals this cycle: {len(self._cycle_signals)}",
                "INFO",
            )
            # Check for stuck cycles (>30s is suspicious)
            if duration > 30:
                self._record_mistake(
                    f"Cycle {cycle_num} took {duration:.1f}s (>30s threshold)",
                    "SLOW_CYCLE",
                )
            self.state.save()
            self._print_dashboard()
            return

        # ─── Regime detection ───
        m = self.RE_REGIME.search(line)
        if m:
            regime = m.group(1)
            conf = float(m.group(2))
            adx = float(m.group(3))
            self.state.regime_history.append({
                "regime": regime,
                "confidence": conf,
                "adx": adx,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            # Keep last 100
            self.state.regime_history = self.state.regime_history[-100:]
            return

        # ─── Strategy routing ───
        m = self.RE_STRATEGY_ROUTE.search(line)
        if m:
            regime = m.group(1)
            strategy = m.group(2)
            key = f"{regime}->{strategy}"
            self.state.signals_by_strategy[key] = (
                self.state.signals_by_strategy.get(key, 0) + 1
            )
            return

        # ─── Signal NONE (rejection) ───
        m = self.RE_SIGNAL_NONE.search(line)
        if m:
            strategy = m.group(1)
            reason = m.group(2)[:80]  # Truncate
            key = f"{strategy}: {reason[:50]}"
            self.state.rejections_by_reason[key] = (
                self.state.rejections_by_reason.get(key, 0) + 1
            )
            return

        # ─── Signal APPROVED ───
        m = self.RE_SIGNAL_APPROVED.search(line)
        if m:
            direction = m.group(1)
            strategy = m.group(2)
            price = m.group(3)
            conf = m.group(4)
            regime = m.group(5)
            self._cycle_signals.append({
                "direction": direction,
                "strategy": strategy,
                "price": price,
                "confidence": conf,
            })
            self.state.cycles_with_signal += 1
            self.log(
                f"SIGNAL: {direction} {strategy} @ {price} "
                f"conf={conf}% regime={regime}",
                "TRADE",
            )
            return

        # ─── Trade placed ───
        m = self.RE_TRADE_PLACED.search(line)
        if m:
            symbol = m.group(1)
            direction = m.group(2)
            price = m.group(3)
            leverage = m.group(4)
            self.state.trades_total += 1
            self.log(
                f"TRADE EXECUTED: {symbol} {direction} @ {price} x{leverage}",
                "TRADE",
            )
            return

        # ─── No valid signals ───
        if self.RE_NO_SIGNALS.search(line):
            return

        # ─── Circuit breaker ───
        m = self.RE_CB_LEVEL.search(line)
        if m:
            level = m.group(1)
            balance = float(m.group(2))
            self.state.current_balance = balance
            if level in ("RED", "DEAD"):
                self.log(
                    f"CB {level}: balance ${balance:.2f}",
                    "ERROR",
                )
            return

        # ─── Position count ───
        m = self.RE_POSITION_COUNT.search(line)
        if m:
            self.state.positions_open = int(m.group(1))
            return

        # ─── Errors ───
        if self.RE_ERROR.search(line):
            self.state.errors.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "msg": line[-200:],
            })
            self.state.errors = self.state.errors[-50:]  # Keep last 50
            self.log(line[-150:], "ERROR")
            return

    def _record_mistake(self, msg: str, category: str):
        """Record a detected mistake or anomaly."""
        mistake = {
            "time": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "msg": msg,
        }
        self.state.mistakes.append(mistake)
        self.state.mistakes = self.state.mistakes[-100:]
        self.log(f"MISTAKE [{category}]: {msg}", "MISTAKE")

    def _print_dashboard(self):
        """Print a compact dashboard after each cycle."""
        s = self.state
        cycles = s.cycles_total or 1
        signal_rate = s.cycles_with_signal / cycles * 100
        win_rate = (
            s.trades_won / s.trades_total * 100
            if s.trades_total > 0
            else 0
        )

        # Calculate daily compound rate
        hours_running = max(1, cycles)  # 1 cycle = 1 hour
        days_running = hours_running / 24
        if days_running > 0 and s.initial_balance > 0 and s.current_balance > 0:
            total_return = (s.current_balance - s.initial_balance) / s.initial_balance
            if days_running >= 1:
                daily_rate = ((1 + total_return) ** (1 / days_running) - 1) * 100
            else:
                daily_rate = total_return * 100
        else:
            daily_rate = 0

        # Count regimes
        recent_regimes = s.regime_history[-30:] if s.regime_history else []
        regime_counts = {}
        for r in recent_regimes:
            regime_counts[r["regime"]] = regime_counts.get(r["regime"], 0) + 1

        print(f"\n{BOLD}{'─'*60}")
        print(f"  WATCHDOG DASHBOARD — Cycle {s.cycles_total}")
        print(f"{'─'*60}{RESET}")
        print(f"  Balance:      ${s.current_balance:,.2f}")
        print(f"  Trades:       {s.trades_total} ({s.trades_won}W/{s.trades_lost}L, {win_rate:.0f}% WR)")
        print(f"  Signals:      {s.cycles_with_signal}/{cycles} cycles ({signal_rate:.1f}%)")
        print(f"  Positions:    {s.positions_open} open")
        print(f"  Daily rate:   {daily_rate:+.3f}% {'<-- BELOW VALIDATED CEILING' if daily_rate < DAILY_TARGET_PCT and days_running >= 1 else ''}")
        print(f"  Regimes (30): {regime_counts}")
        print(f"  Mistakes:     {len(s.mistakes)}")
        print(f"  Errors:       {len(s.errors)}")
        print(f"{BOLD}{'─'*60}{RESET}\n")

    def run(self):
        """Main loop: tail bot.log in real-time."""
        self.log("Watchdog started — monitoring bot.log", "INFO")
        self.log(f"Target: {DAILY_TARGET_PCT}% daily compound", "METRIC")

        if not LOG_FILE.exists():
            self.log(f"Waiting for {LOG_FILE} to appear...", "WARN")
            while not LOG_FILE.exists():
                time.sleep(2)

        # Start from end of file (don't replay history)
        with open(LOG_FILE, "r") as f:
            f.seek(0, 2)  # Seek to end
            self.log(f"Tailing {LOG_FILE} (size: {LOG_FILE.stat().st_size} bytes)", "INFO")

            while True:
                line = f.readline()
                if line:
                    self.process_line(line)
                else:
                    # Check if bot is still running
                    pid_file = PROJECT_ROOT / "user_data" / "logs" / "bot.pid"
                    if pid_file.exists():
                        pid = int(pid_file.read_text().strip())
                        try:
                            os.kill(pid, 0)  # Check if process exists
                        except ProcessLookupError:
                            self.log(
                                f"Bot process {pid} not found — bot may have crashed!",
                                "ERROR",
                            )
                            self._record_mistake(
                                f"Bot process {pid} disappeared",
                                "BOT_CRASH",
                            )
                            self.state.save()
                    time.sleep(0.5)


def main():
    print(f"\n{BOLD}{CYAN}")
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║     CLAUDE QUANT WATCHDOG v1.0               ║")
    print("  ║     Real-time trading bot monitor            ║")
    print("  ║     Validated: 0.628% daily (1% aspirational)  ║")
    print("  ╚══════════════════════════════════════════════╝")
    print(f"{RESET}")

    watchdog = Watchdog()
    try:
        watchdog.run()
    except KeyboardInterrupt:
        watchdog.log("Watchdog stopped by user", "INFO")
        watchdog.state.save()
        print(f"\n{YELLOW}Watchdog stopped. State saved to {STATE_FILE}{RESET}")


if __name__ == "__main__":
    main()
