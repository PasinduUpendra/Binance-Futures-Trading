#!/usr/bin/env python3
"""Claude Quant Paper Trading Bot — Testnet Runner.

Runs the orchestrator continuously with 1-hour cycles.
Logs to both console and file.
"""

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load env before any imports that use it
load_dotenv()

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.orchestrator.main import Orchestrator


def setup_logging() -> None:
    """Configure dual logging: console + file."""
    log_dir = PROJECT_ROOT / "user_data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    # File handler
    file_handler = logging.FileHandler(
        log_dir / "bot.log", encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)

    # Reduce noise
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    logger = logging.getLogger("claude_quant.runner")

    logger.info("=" * 60)
    logger.info("Claude Quant Paper Trading Bot — Starting")
    logger.info("Mode: TESTNET (paper trading)")
    logger.info("=" * 60)

    orchestrator = Orchestrator()
    await orchestrator.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested. Goodbye.")
