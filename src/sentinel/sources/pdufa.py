from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import structlog

from src.core.clock import utcnow_naive
from src.core.config import SentinelConfig
from src.sentinel.models import (
    DIRECTION_UNCLEAR,
    MARKET_EQUITY,
    PRIORITY_CRITICAL,
    SOURCE_PDUFA,
    EventSource,
    RawEvent,
)
from src.sentinel.universe import UniverseIndex

PDUFA_PATH = Path("data/pdufa_calendar.csv")


@dataclass(frozen=True)
class PdufaEntry:
    ticker: str
    pdufa_date: date
    drug: str
    indication: str
    phase: str

    @property
    def dedup_key(self) -> str:
        return f"pdufa:{self.ticker}:{self.pdufa_date.isoformat()}"


def load_pdufa_calendar(path: Path | str = PDUFA_PATH) -> list[PdufaEntry]:
    path = Path(path)
    logger = structlog.get_logger("PdufaSource")

    if not path.exists():
        logger.warning(
            "pdufa_calendar_missing",
            path=str(path),
            note="no free reliable API exists — this file is maintained by hand",
        )
        return []

    entries: list[PdufaEntry] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip().upper()
            raw_date = (row.get("pdufa_date") or "").strip()
            if not ticker or not raw_date:
                continue
            try:
                parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                logger.warning("pdufa_bad_date", ticker=ticker, value=raw_date)
                continue

            entries.append(PdufaEntry(
                ticker=ticker,
                pdufa_date=parsed,
                drug=(row.get("drug") or "").strip(),
                indication=(row.get("indication") or "").strip(),
                phase=(row.get("phase") or "").strip(),
            ))

    if not entries:
        logger.warning("pdufa_calendar_empty", path=str(path))
    return entries


def in_window(entry: PdufaEntry, today: date, window: tuple[int, int]) -> bool:
    days_since_pdufa = (today - entry.pdufa_date).days
    return window[0] <= days_since_pdufa <= window[1]


class PdufaSource(EventSource):

    name = "pdufa_calendar"
    market = MARKET_EQUITY

    def __init__(
        self,
        universe: UniverseIndex,
        config: SentinelConfig,
        path: Path | str = PDUFA_PATH,
    ) -> None:
        self._universe = universe
        self._config = config
        self._path = Path(path)
        self._logger = structlog.get_logger("PdufaSource")
        self._seen: set[str] = set()

    async def poll(self) -> list[RawEvent]:
        entries = load_pdufa_calendar(self._path)
        if not entries:
            return []

        today = utcnow_naive().date()
        window = tuple(self._config.pdufa_window_days)

        events: list[RawEvent] = []
        for entry in entries:
            if entry.dedup_key in self._seen:
                continue
            if entry.ticker not in self._universe:
                continue
            if not in_window(entry, today, window):
                continue

            self._seen.add(entry.dedup_key)
            days_out = (entry.pdufa_date - today).days

            events.append(RawEvent(
                source=SOURCE_PDUFA,
                ticker=entry.ticker,
                market=MARKET_EQUITY,
                dedup_key=entry.dedup_key,
                priority=PRIORITY_CRITICAL,
                direction=DIRECTION_UNCLEAR,
                payload={
                    "pdufa_date": entry.pdufa_date.isoformat(),
                    "days_until": days_out,
                    "drug": entry.drug,
                    "indication": entry.indication,
                    "phase": entry.phase,
                    "binary_event": True,
                },
            ))

        if events:
            self._logger.info("pdufa_events", count=len(events))
        return events
