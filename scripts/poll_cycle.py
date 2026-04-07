#!/usr/bin/env python3
"""Poll bot.log until new lines appear (Cycle 2+)."""
import time
import sys

target = int(sys.argv[1]) if len(sys.argv) > 1 else 858
logfile = "user_data/logs/bot.log"

print(f"Polling {logfile} for lines > {target}...")
while True:
    with open(logfile) as f:
        lines = f.readlines()
    if len(lines) > target:
        print(f"LOG GREW to {len(lines)} lines")
        for line in lines[target:]:
            print(line.rstrip())
        break
    time.sleep(10)
