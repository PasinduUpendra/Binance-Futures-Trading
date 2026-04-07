#!/usr/bin/env python3
"""Monitor bot: health check then wait for Cycle 3."""
import time
import subprocess
import sys
import os

LOG = "user_data/logs/bot.log"
TARGET = 1190
BOT_PID = "97418"

KEYWORDS = [
    "Cycle", "Regime", "Adaptive signal", "consensus", "Best signal",
    "Already", "funding", "FUNDING", "Execute", "ORDER", "balance",
    "circuit_breaker", "confidence", "leverage", "position_sizer",
    "trailing", "Supertrend", "dead zone", "positioned", "SL", "TP",
    "margin", "GREEN", "YELLOW", "RED", "DEAD"
]

def check_health():
    r = subprocess.run(["ps", "-p", BOT_PID, "-o", "etime="],
                       capture_output=True, text=True)
    uptime = r.stdout.strip() or "DEAD"
    with open(LOG) as f:
        count = sum(1 for _ in f)
    print(f"[HEALTH {time.strftime('%H:%M:%S')}] lines={count} uptime={uptime}")
    sys.stdout.flush()
    return count

def wait_for_new_lines(target, max_wait=3600):
    start = time.time()
    while time.time() - start < max_wait:
        with open(LOG) as f:
            count = sum(1 for _ in f)
        if count > target:
            with open(LOG) as f:
                lines = f.readlines()
            new = lines[target:]
            print(f"\n[NEW LINES {time.strftime('%H:%M:%S')}] {len(new)} lines added:")
            for line in new:
                l = line.rstrip()
                if any(k in l for k in KEYWORDS):
                    print(l)
            sys.stdout.flush()
            return count
        time.sleep(10)
    print(f"[TIMEOUT {time.strftime('%H:%M:%S')}] still {count} lines")
    sys.stdout.flush()
    return count

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(f"[START {time.strftime('%H:%M:%S')}] Monitoring bot PID {BOT_PID}")
    sys.stdout.flush()
    
    # Initial health check
    check_health()
    
    # Wait for Cycle 3
    print(f"[WAITING] For Cycle 3 (target > {TARGET} lines)...")
    sys.stdout.flush()
    new_count = wait_for_new_lines(TARGET)
    
    # Final health check
    check_health()
    print("[DONE]")
    sys.stdout.flush()
