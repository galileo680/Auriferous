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
    EarningsCalendarSource,
    EdgarFilingSource,
    HaltSource,
    PdufaSource,
    VolumeAnomalySource,
)
from src.sentinel.sources.earnings import refresh_calendar
from src.sentinel.universe import UniverseIndex, load_universe
from src.triage.agent import TriageAgent
from src.triage.budget import TriageBudget
from src.triage.context import ContextBuilder
from src.triage.loop import TriageLoop
from src.swarm.agents import SwarmAgents
from src.swarm.evidence import EvidenceCollector
from src.swarm.loop import SwarmLoop
from src.broker.ibkr import IBKRClient
from src.core.market_clock import MarketClock
from src.database.repositories.equity import EquityRepository
from src.executor.engine import ExecutionEngine
from src.executor.loop import ExecutorLoop
from src.positions.manager import PositionManager
from src.positions.reconcile import ReconcileLoop
from src.risk.budget import daily_llm_budget
from src.risk.drawdown import DrawdownTracker
from src.risk.governor import RiskGovernor
from src.risk.loop import GovernorLoop
from src.shadow.book import ShadowBookService
from src.shadow.calibrator import Calibrator
from src.shadow.loop import CalibratorLoop, ShadowSyncLoop
from src.shadow.prices import fetch_prices
from src.structurer.loop import StructurerLoop

logger = structlog.get_logger("Auriferous")

JOB_SENTINEL = "sentinel"
JOB_TRIAGE = "triage"
JOB_SWARM = "swarm"
JOB_STRUCTURER = "structurer"
JOB_GOVERNOR = "governor"
JOB_EXECUTOR = "executor"
JOB_POSITIONS = "positions"
JOB_RECONCILE = "reconcile"
JOB_SHADOW = "shadow_sync"
JOB_CALIBRATION = "calibration"
JOB_PDUFA = "pdufa_refresh"
JOB_EARNINGS = "earnings_refresh"
JOB_UNIVERSE = "universe_refresh"

TIMEOUT_SENTINEL = 300.0
TIMEOUT_TRIAGE = 900.0
TIMEOUT_SWARM = 1800.0
TIMEOUT_STRUCTURER = 900.0
TIMEOUT_GOVERNOR = 300.0
TIMEOUT_EXECUTOR = 600.0
TIMEOUT_POSITIONS = 600.0
TIMEOUT_RECONCILE = 120.0
TIMEOUT_SHADOW = 600.0
TIMEOUT_CALIBRATION = 600.0
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
    triage: TriageLoop | None,
    swarm: SwarmLoop | None,
    structurer: StructurerLoop | None,
    governor: GovernorLoop,
    executor: ExecutorLoop | None,
    positions: PositionManager | None,
    reconcile: ReconcileLoop | None,
    shadow: ShadowSyncLoop | None,
    calibration: CalibratorLoop,
    universe: UniverseIndex,
) -> None:

    async def run_sentinel() -> None:
        await sentinel.run()

    async def run_triage() -> None:
        await triage.run()

    async def run_swarm() -> None:
        await swarm.run()

    async def run_structurer() -> None:
        await structurer.run()

    async def run_governor() -> None:
        await governor.run()

    async def run_executor() -> None:
        await executor.run()

    async def run_positions() -> None:
        await positions.run()

    async def run_reconcile() -> None:
        await reconcile.run()

    async def run_shadow() -> None:
        await shadow.run()

    async def run_calibration() -> None:
        await calibration.run()

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
        job_id=JOB_TRIAGE,
        callback=run_triage,
        interval_seconds=config.jobs.triage_seconds,
        max_timeout=TIMEOUT_TRIAGE,
        enabled=triage is not None,
    )
    scheduler.register(
        job_id=JOB_SWARM,
        callback=run_swarm,
        interval_seconds=config.jobs.swarm_seconds,
        max_timeout=TIMEOUT_SWARM,
        enabled=swarm is not None,
    )
    scheduler.register(
        job_id=JOB_STRUCTURER,
        callback=run_structurer,
        interval_seconds=config.jobs.structurer_seconds,
        max_timeout=TIMEOUT_STRUCTURER,
        enabled=structurer is not None,
    )
    scheduler.register(
        job_id=JOB_GOVERNOR,
        callback=run_governor,
        interval_seconds=config.jobs.governor_seconds,
        max_timeout=TIMEOUT_GOVERNOR,
    )
    scheduler.register(
        job_id=JOB_EXECUTOR,
        callback=run_executor,
        interval_seconds=config.jobs.executor_seconds,
        max_timeout=TIMEOUT_EXECUTOR,
        enabled=executor is not None,
    )
    scheduler.register(
        job_id=JOB_POSITIONS,
        callback=run_positions,
        interval_seconds=config.jobs.position_seconds,
        max_timeout=TIMEOUT_POSITIONS,
        enabled=positions is not None,
    )
    scheduler.register(
        job_id=JOB_RECONCILE,
        callback=run_reconcile,
        interval_seconds=config.jobs.reconcile_seconds,
        max_timeout=TIMEOUT_RECONCILE,
        enabled=reconcile is not None,
    )
    scheduler.register(
        job_id=JOB_SHADOW,
        callback=run_shadow,
        interval_seconds=config.jobs.shadow_seconds,
        max_timeout=TIMEOUT_SHADOW,
        enabled=shadow is not None,
    )
    scheduler.register(
        job_id=JOB_CALIBRATION,
        callback=run_calibration,
        interval_seconds=config.jobs.calibration_seconds,
        max_timeout=TIMEOUT_CALIBRATION,
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


def _build_triage(config: AuriferousConfig, universe: UniverseIndex) -> TriageLoop | None:
    if not config.llm.api_key:
        return None

    from langchain_openai import ChatOpenAI

    fast_llm = ChatOpenAI(
        model=config.llm.fast_model,
        temperature=config.llm.temperature,
        api_key=config.llm.api_key,
        timeout=config.llm.timeout_seconds,
    )

    return TriageLoop(
        agent=TriageAgent(fast_llm),
        context_builder=ContextBuilder(universe),
        budget=TriageBudget(config.triage),
        config=config.triage,
    )


def _build_swarm(config: AuriferousConfig, universe: UniverseIndex) -> SwarmLoop | None:
    if not config.llm.api_key:
        return None

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=config.llm.temperature,
        api_key=config.llm.api_key,
        timeout=config.llm.timeout_seconds,
    )

    return SwarmLoop(
        agents=SwarmAgents(
            llm,
            cost_per_1m_input=config.llm.cost_per_1m_input,
            cost_per_1m_output=config.llm.cost_per_1m_output,
        ),
        collector=EvidenceCollector(universe, fetch_filings=config.swarm.fetch_filing_text),
        config=config.swarm,
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

    if config.system.mode == "live":
        async with db.session() as session:
            latest = await EquityRepository(session).get_latest()
        live_equity = float(latest.equity) if latest else config.capital.initial_usd
        config.swarm.max_cost_usd_per_day = daily_llm_budget(
            live_equity, config.system.mode, config.swarm.max_cost_usd_per_day
        )
        logger.info(
            "llm_budget_live",
            equity=round(live_equity, 2),
            max_cost_usd_per_day=config.swarm.max_cost_usd_per_day,
        )

    edgar = EdgarClient(config.sentinel.contact_email)
    sentinel = SentinelLoop([
        EdgarFilingSource(edgar, universe, config.sentinel),
        HaltSource(universe, config.sentinel),
        PdufaSource(universe, config.sentinel),
        VolumeAnomalySource(universe, config.sentinel),
        EarningsCalendarSource(universe),
    ])

    triage = _build_triage(config, universe)
    swarm = _build_swarm(config, universe)
    if triage is None or swarm is None:
        logger.warning(
            "llm_stages_disabled",
            note="no OPENAI_API_KEY — sentinel will collect events but nothing will analyse them",
        )

    broker = IBKRClient(config.broker)
    structurer: StructurerLoop | None = None
    executor: ExecutorLoop | None = None
    positions: PositionManager | None = None
    reconcile: ReconcileLoop | None = None
    try:
        await broker.connect()
        clock = MarketClock()
        engine = ExecutionEngine(broker)
        structurer = StructurerLoop(broker, config.structurer, config.capital.initial_usd)
        executor = ExecutorLoop(broker, engine, clock)
        positions = PositionManager(
            broker,
            engine,
            config.positions,
            clock,
            DrawdownTracker(config.risk, config.capital.initial_usd),
        )
        reconcile = ReconcileLoop(broker)
    except Exception as e:
        logger.warning(
            "broker_stages_disabled",
            error=str(e),
            note="IB Gateway unreachable — analysis will run but nothing will be built or executed",
        )

    governor = GovernorLoop(RiskGovernor(config.risk, config.capital, universe))

    shadow: ShadowSyncLoop | None = None
    if config.shadow.enabled:
        shadow = ShadowSyncLoop(ShadowBookService(
            prices=fetch_prices,
            broker=broker if broker.is_connected() else None,
            parallel_enabled=config.shadow.parallel_book_enabled,
        ))
    calibration = CalibratorLoop(Calibrator())

    scheduler = SchedulerManager.get_instance()
    _register_jobs(
        scheduler, config, sentinel, triage, swarm, structurer, governor, executor,
        positions, reconcile, shadow, calibration, universe,
    )

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
        if broker.is_connected():
            await broker.disconnect()
        await db.close()
        logger.info("auriferous_stopped")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    try:
        asyncio.run(main(path))
    except KeyboardInterrupt:
        pass
