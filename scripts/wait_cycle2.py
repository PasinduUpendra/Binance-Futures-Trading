#!/usr/bin/env python3
"""Wait for bot.log to grow past target line count, then print new lines."""
import time
import sys

LOG = "user_data/logs/bot.log"
TARGET = 1190  # After Cycle 2
MAX_WAIT = 3600  # 60 minutes

start = time.time()
while time.time() - start < MAX_WAIT:
    with open(LOG) as f:
        count = sum(1 for _ in f)
    if count > TARGET:
        with open(LOG) as f:
            lines = f.readlines()
        new_lines = lines[TARGET:]
        print(f"DETECTED {len(new_lines)} new lines at {time.strftime('%H:%M:%S')}:")
        for line in new_lines[:120]:
            sys.stdout.write(line)
        sys.stdout.flush()
        sys.exit(0)
    time.sleep(8)

print(f"TIMEOUT at {time.strftime('%H:%M:%S')}: still {count} lines")
sys.exit(1)
