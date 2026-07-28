from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import AuriferousConfig, ConfigLoader
from src.core.scheduler import SchedulerManager
from src.database.session import DatabaseManager
from src.dataflows.edgar import EdgarClient
from src.dataflows.pdufa_harvester import PdufaHarvester
from src.sentinel.loop import SentinelLoop
from src.sentinel.sources import (
    CryptoFlowSource,
    EarningsCalendarSource,
    EdgarFilingSource,
    HaltSource,
    PdufaSource,
    VolumeAnomalySource,
)
from src.sentinel.sources.earnings import refresh_calendar
from src.sentinel.universe import UniverseIndex, load_universe

logger = structlog.get_logger("Auriferous")

JOB_SENTINEL = "sentinel"
JOB_PDUFA = "pdufa_refresh"
JOB_EARNINGS = "earnings_refresh"
JOB_UNIVERSE = "universe_refresh"

TIMEOUT_SENTINEL = 300.0
TIMEOUT_PDUFA = 3600.0
TIMEOUT_EARNINGS = 3600.0
TIMEOUT_UNIVERSE = 14400.0


def _configure_logging(config: AuriferousConfig) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(config.system.log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _register_jobs(
    scheduler: SchedulerManager,
    config: AuriferousConfig,
    sentinel: SentinelLoop,
    universe: UniverseIndex,
) -> None:

    async def run_sentinel() -> None:
        await sentinel.run()

    async def run_pdufa_refresh() -> None:
        from scripts.refresh_pdufa import main as refresh_pdufa_main

        await refresh_pdufa_main(
            config_path="config/auriferous.yaml",
            lookback_days=config.jobs.pdufa_lookback_days,
        )

    async def run_earnings_refresh() -> None:
        await refresh_calendar(universe.tickers)

    async def run_universe_refresh() -> None:
        from scripts.refresh_universe import main as refresh_universe_main

        await refresh_universe_main(config_path="config/auriferous.yaml")

    scheduler.register(
        job_id=JOB_SENTINEL,
        callback=run_sentinel,
        interval_seconds=config.jobs.sentinel_seconds,
        max_timeout=TIMEOUT_SENTINEL,
    )
    scheduler.register(
        job_id=JOB_PDUFA,
        callback=run_pdufa_refresh,
        interval_seconds=config.jobs.pdufa_refresh_seconds,
        max_timeout=TIMEOUT_PDUFA,
    )
    scheduler.register(
        job_id=JOB_EARNINGS,
        callback=run_earnings_refresh,
        interval_seconds=config.jobs.earnings_refresh_seconds,
        max_timeout=TIMEOUT_EARNINGS,
    )
    scheduler.register(
        job_id=JOB_UNIVERSE,
        callback=run_universe_refresh,
        interval_seconds=config.jobs.universe_refresh_seconds,
        max_timeout=TIMEOUT_UNIVERSE,
        enabled=config.jobs.universe_refresh_enabled,
        run_on_start=False,
    )


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    _configure_logging(config)

    logger.info(
        "auriferous_starting",
        mode=config.system.mode,
        capital_usd=config.capital.initial_usd,
        client_id=config.broker.client_id,
    )

    entries = load_universe()
    if not entries:
        logger.error(
            "universe_empty",
            note="run scripts/refresh_universe.py once before the first start",
        )
        return

    universe = UniverseIndex(entries)

    db = DatabaseManager.get_instance(config)
    if not await db.health_check():
        logger.error("database_unreachable", database=config.database.name)
        return
    await db.create_tables()

    edgar = EdgarClient(config.sentinel.contact_email)
    sentinel = SentinelLoop([
        EdgarFilingSource(edgar, universe, config.sentinel),
        HaltSource(universe, config.sentinel),
        PdufaSource(universe, config.sentinel),
        VolumeAnomalySource(universe, config.sentinel),
        EarningsCalendarSource(universe),
        CryptoFlowSource(),
    ])

    scheduler = SchedulerManager.get_instance()
    _register_jobs(scheduler, config, sentinel, universe)

    try:
        await scheduler.start()
        logger.info("auriferous_running", universe=len(universe))
        await scheduler.wait_for_shutdown()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    finally:
        await scheduler.stop(timeout=30.0)
        await sentinel.close()
        await edgar.close()
        await db.close()
        logger.info("auriferous_stopped")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    try:
        asyncio.run(main(path))
    except KeyboardInterrupt:
        pass
