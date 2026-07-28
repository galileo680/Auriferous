from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import structlog

from src.core.clock import utcnow_naive
from src.sentinel.models import (
    DIRECTION_UNCLEAR,
    MARKET_EQUITY,
    PRIORITY_LOW,
    SOURCE_EARNINGS_CALENDAR,
    EventSource,
    RawEvent,
)
from src.sentinel.universe import UniverseIndex

CALENDAR_PATH = Path("data/earnings_calendar.csv")
PRE_EVENT_DAYS = 2
POST_EVENT_DAYS = 1
STALE_AFTER_HOURS = 24


@dataclass(frozen=True)
class EarningsEntry:
    ticker: str
    report_date: date

    @property
    def dedup_key(self) -> str:
        return f"earnings:{self.ticker}:{self.report_date.isoformat()}"


def load_calendar(path: Path | str = CALENDAR_PATH) -> list[EarningsEntry]:
    path = Path(path)
    if not path.exists():
        return []

    entries: list[EarningsEntry] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip().upper()
            raw = (row.get("report_date") or "").strip()
            if not ticker or not raw:
                continue
            try:
                entries.append(EarningsEntry(ticker, datetime.strptime(raw, "%Y-%m-%d").date()))
            except ValueError:
                continue
    return entries


def save_calendar(entries: list[EarningsEntry], path: Path | str = CALENDAR_PATH) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("ticker", "report_date"))
        writer.writeheader()
        for entry in sorted(entries, key=lambda e: (e.report_date, e.ticker)):
            writer.writerow({
                "ticker": entry.ticker,
                "report_date": entry.report_date.isoformat(),
            })
    return len(entries)


def classify_proximity(entry: EarningsEntry, today: date) -> str | None:
    delta = (entry.report_date - today).days
    if 0 < delta <= PRE_EVENT_DAYS:
        return "PRE_EVENT"
    if -POST_EVENT_DAYS <= delta <= 0:
        return "POST_EVENT"
    return None


def calendar_is_stale(path: Path | str = CALENDAR_PATH, max_age_hours: int = STALE_AFTER_HOURS) -> bool:
    path = Path(path)
    if not path.exists():
        return True
    age = datetime.utcnow() - datetime.utcfromtimestamp(path.stat().st_mtime)
    return age > timedelta(hours=max_age_hours)


class EarningsCalendarSource(EventSource):

    name = "earnings_calendar"
    market = MARKET_EQUITY

    def __init__(self, universe: UniverseIndex, path: Path | str = CALENDAR_PATH) -> None:
        self._universe = universe
        self._path = Path(path)
        self._logger = structlog.get_logger("EarningsCalendarSource")
        self._seen: set[str] = set()

    async def poll(self) -> list[RawEvent]:
        if calendar_is_stale(self._path):
            self._logger.warning(
                "earnings_calendar_stale",
                path=str(self._path),
                note="run scripts/refresh_calendars.py",
            )

        entries = load_calendar(self._path)
        if not entries:
            return []

        today = utcnow_naive().date()
        events: list[RawEvent] = []

        for entry in entries:
            if entry.dedup_key in self._seen:
                continue
            if entry.ticker not in self._universe:
                continue

            proximity = classify_proximity(entry, today)
            if proximity is None:
                continue

            self._seen.add(entry.dedup_key)
            events.append(RawEvent(
                source=SOURCE_EARNINGS_CALENDAR,
                ticker=entry.ticker,
                market=MARKET_EQUITY,
                dedup_key=entry.dedup_key,
                priority=PRIORITY_LOW,
                direction=DIRECTION_UNCLEAR,
                payload={
                    "report_date": entry.report_date.isoformat(),
                    "proximity": proximity,
                    "days_until": (entry.report_date - today).days,
                },
            ))

        if events:
            self._logger.info("earnings_calendar_events", count=len(events))
        return events


async def refresh_calendar(tickers: list[str], path: Path | str = CALENDAR_PATH) -> int:
    logger = structlog.get_logger("EarningsCalendarRefresh")

    def fetch() -> list[EarningsEntry]:
        import pandas as pd
        import yfinance as yf

        collected: list[EarningsEntry] = []
        horizon = datetime.utcnow().date() + timedelta(days=90)

        for ticker in tickers:
            try:
                dates = yf.Ticker(ticker).earnings_dates
                if dates is None or dates.empty:
                    continue
                for stamp in dates.index:
                    value = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
                    if pd.isna(value):
                        continue
                    report_date = value.date()
                    if datetime.utcnow().date() <= report_date <= horizon:
                        collected.append(EarningsEntry(ticker, report_date))
            except Exception:
                continue

        return collected

    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, fetch)
    written = save_calendar(entries, path)
    logger.info("earnings_calendar_refreshed", tickers=len(tickers), entries=written)
    return written
