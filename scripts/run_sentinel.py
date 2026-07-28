from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import ConfigLoader
from src.database.session import DatabaseManager
from src.dataflows.edgar import EdgarClient
from src.sentinel.loop import SentinelLoop
from src.sentinel.sources import (
    CryptoFlowSource,
    EarningsCalendarSource,
    EdgarFilingSource,
    HaltSource,
    PdufaSource,
    VolumeAnomalySource,
)
from src.sentinel.universe import UniverseIndex, load_universe

logger = structlog.get_logger("RunSentinel")


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def build_sentinel(config, universe: UniverseIndex, edgar: EdgarClient) -> SentinelLoop:
    return SentinelLoop([
        EdgarFilingSource(edgar, universe, config.sentinel),
        HaltSource(universe, config.sentinel),
        PdufaSource(universe, config.sentinel),
        VolumeAnomalySource(universe, config.sentinel),
        EarningsCalendarSource(universe),
        CryptoFlowSource(),
    ])


async def main(config_path: str = "config/auriferous.yaml", cycles: int = 0) -> None:
    config = ConfigLoader.load(config_path=config_path)
    _configure_logging(config.system.log_level)

    entries = load_universe()
    if not entries:
        logger.error(
            "universe_empty",
            note="run scripts/refresh_universe.py before starting the sentinel",
        )
        return

    universe = UniverseIndex(entries)
    db = DatabaseManager.get_instance(config)
    if not await db.health_check():
        logger.error("database_unreachable", database=config.database.name)
        return

    edgar = EdgarClient(config.sentinel.contact_email)
    sentinel = build_sentinel(config, universe, edgar)

    logger.info(
        "sentinel_start",
        universe=len(universe),
        poll_seconds=config.sentinel.poll_seconds,
        cycles="infinite" if cycles == 0 else cycles,
    )

    completed = 0
    try:
        while cycles == 0 or completed < cycles:
            await sentinel.run()
            completed += 1
            if cycles == 0 or completed < cycles:
                await asyncio.sleep(config.sentinel.poll_seconds)
    except KeyboardInterrupt:
        logger.info("sentinel_interrupted", cycles_completed=completed)
    finally:
        await sentinel.close()
        await edgar.close()
        await db.close()

    logger.info("sentinel_stopped", cycles_completed=completed)


if __name__ == "__main__":
    path = "config/auriferous.yaml"
    limit = 0
    for arg in sys.argv[1:]:
        if arg.startswith("--cycles="):
            limit = int(arg.split("=", 1)[1])
        elif not arg.startswith("--"):
            path = arg
    asyncio.run(main(path, limit))
