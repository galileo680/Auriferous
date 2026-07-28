from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from src.database.models import EVENT_STATUS_NEW, Event
from src.database.repositories import EventRepository
from src.database.session import DatabaseManager
from src.sentinel.models import EventSource, RawEvent

DEDUP_WINDOW_HOURS = 24


@dataclass
class SentinelResult:
    polled: int = 0
    emitted: int = 0
    duplicates: int = 0
    persisted: int = 0
    errors: int = 0
    by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def merge_source(self, name: str, count: int) -> None:
        self.by_source[name] += count


class SentinelLoop:

    def __init__(self, sources: list[EventSource]) -> None:
        self._sources = sources
        self._logger = structlog.get_logger("SentinelLoop")
        self._memory_dedup: set[str] = set()

    async def run(self) -> SentinelResult:
        result = SentinelResult()

        outcomes = await asyncio.gather(
            *(self._poll_source(source) for source in self._sources),
            return_exceptions=True,
        )

        collected: list[RawEvent] = []
        for source, outcome in zip(self._sources, outcomes):
            if isinstance(outcome, Exception):
                result.errors += 1
                self._logger.error(
                    "sentinel_source_failed", source=source.name, error=str(outcome)
                )
                continue

            result.polled += 1
            result.merge_source(source.name, len(outcome))
            collected.extend(outcome)

        result.emitted = len(collected)
        if collected:
            result.persisted, result.duplicates = await self._persist(collected)

        if result.emitted or result.errors:
            self._logger.info(
                "sentinel_cycle",
                sources_polled=result.polled,
                emitted=result.emitted,
                persisted=result.persisted,
                duplicates=result.duplicates,
                errors=result.errors,
                by_source=dict(result.by_source),
            )
        return result

    async def _poll_source(self, source: EventSource) -> list[RawEvent]:
        return await source.poll()

    async def _persist(self, events: list[RawEvent]) -> tuple[int, int]:
        db = DatabaseManager.get_instance()
        persisted = 0
        duplicates = 0

        async with db.session() as session:
            repo = EventRepository(session)

            for event in self._deduplicate_in_batch(events):
                if event.dedup_key in self._memory_dedup:
                    duplicates += 1
                    continue

                if await repo.exists_dedup_key(event.dedup_key, DEDUP_WINDOW_HOURS):
                    self._memory_dedup.add(event.dedup_key)
                    duplicates += 1
                    continue

                await repo.create(Event(
                    source=event.source,
                    ticker=event.ticker,
                    market=event.market,
                    direction=event.direction,
                    payload=event.payload,
                    raw_text=event.raw_text,
                    priority=event.priority,
                    dedup_key=event.dedup_key,
                    status=EVENT_STATUS_NEW,
                    detected_at=event.detected_at,
                ))
                self._memory_dedup.add(event.dedup_key)
                persisted += 1

        return persisted, duplicates

    @staticmethod
    def _deduplicate_in_batch(events: list[RawEvent]) -> list[RawEvent]:
        best: dict[str, RawEvent] = {}
        for event in events:
            existing = best.get(event.dedup_key)
            if existing is None or event.priority < existing.priority:
                best[event.dedup_key] = event
        return sorted(best.values(), key=lambda e: (e.priority, e.detected_at))

    async def close(self) -> None:
        for source in self._sources:
            try:
                await source.close()
            except Exception as e:
                self._logger.warning(
                    "sentinel_source_close_failed", source=source.name, error=str(e)
                )
