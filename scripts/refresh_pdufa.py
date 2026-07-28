from __future__ import annotations

import asyncio
import csv
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import ConfigLoader
from src.dataflows.pdufa_harvester import PdufaDate, PdufaHarvester
from src.sentinel.sources.pdufa import PDUFA_PATH
from src.sentinel.universe import UniverseIndex, load_universe

logger = structlog.get_logger("RefreshPdufa")

FIELDNAMES = ("ticker", "pdufa_date", "drug", "indication", "phase", "source_accession")


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


def write_calendar(entries: list[PdufaDate], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "ticker": entry.ticker,
                "pdufa_date": entry.pdufa_date.isoformat(),
                "drug": entry.drug,
                "indication": entry.indication,
                "phase": entry.phase,
                "source_accession": entry.source_accession,
            })

    return len(entries)


async def main(config_path: str = "config/auriferous.yaml", lookback_days: int = 180) -> None:
    config = ConfigLoader.load(config_path=config_path)
    _configure_logging(config.system.log_level)

    entries = load_universe()
    universe = UniverseIndex(entries) if entries else None
    if universe is None:
        logger.warning(
            "universe_missing",
            note="writing every discovered date — run refresh_universe.py to filter",
        )

    harvester = PdufaHarvester(config.sentinel.contact_email)
    try:
        discovered = await harvester.harvest(lookback_days)
    finally:
        await harvester.close()

    if universe is not None:
        in_universe = [d for d in discovered if d.ticker in universe]
        skipped = len(discovered) - len(in_universe)
    else:
        in_universe = discovered
        skipped = 0

    written = write_calendar(in_universe, PDUFA_PATH)

    logger.info(
        "pdufa_calendar_written",
        path=str(PDUFA_PATH),
        entries=written,
        skipped_outside_universe=skipped,
        lookback_days=lookback_days,
    )

    for entry in in_universe[:15]:
        logger.info(
            "pdufa_upcoming",
            ticker=entry.ticker,
            date=entry.pdufa_date.isoformat(),
            source=entry.source_accession,
        )


if __name__ == "__main__":
    path = "config/auriferous.yaml"
    lookback = 180
    for arg in sys.argv[1:]:
        if arg.startswith("--lookback="):
            lookback = int(arg.split("=", 1)[1])
        elif not arg.startswith("--"):
            path = arg
    asyncio.run(main(path, lookback))
