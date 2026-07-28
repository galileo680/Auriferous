from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.clock import utcnow_naive
from src.database.models import (
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_NEW,
    EVENT_STATUS_QUEUED,
    EVENT_STATUS_TRADED,
    Base,
    Event,
)
from src.database.repositories import EventRepository
from src.sentinel.universe import SECTOR_UNKNOWN, UniverseEntry, normalize_sector


def run_with_db(body):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await body(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def make_event(key: str = "k1", age_hours: float = 0.0, status: str = EVENT_STATUS_NEW) -> Event:
    return Event(
        source="EDGAR_8K",
        ticker="ACME",
        market="EQUITY",
        payload={"items": ["2.02"]},
        priority=2,
        dedup_key=key,
        status=status,
        detected_at=utcnow_naive() - timedelta(hours=age_hours),
    )


def test_queue_and_release_round_trip():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event())

        await repo.queue_for_open(event.id)
        queued = await repo.get_queued()

        released = await repo.release_queued()
        pending = await repo.get_pending()

        return len(queued), released, [e.status for e in pending]

    queued, released, statuses = run_with_db(body)
    assert queued == 1
    assert released == 1
    assert statuses == [EVENT_STATUS_NEW]


def test_queued_events_do_not_show_up_as_pending():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event())
        await repo.queue_for_open(event.id)
        return await repo.get_pending()

    assert run_with_db(body) == []


def test_release_clears_the_processed_timestamp():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event())
        await repo.queue_for_open(event.id)
        await repo.release_queued()
        return (await repo.get_by_id(event.id)).processed_at

    assert run_with_db(body) is None


def test_stale_events_expire_in_both_pending_states():
    async def body(session):
        repo = EventRepository(session)
        await repo.create(make_event("fresh", age_hours=2))
        await repo.create(make_event("old_new", age_hours=20))
        await repo.create(make_event("old_queued", age_hours=25, status=EVENT_STATUS_QUEUED))

        expired = await repo.expire_stale(max_age_hours=18)
        remaining = {e.dedup_key for e in await repo.get_pending()}
        return expired, remaining

    expired, remaining = run_with_db(body)
    assert expired == 2
    assert remaining == {"fresh"}


def test_expiry_does_not_touch_already_traded_events():
    async def body(session):
        repo = EventRepository(session)
        await repo.create(make_event("traded", age_hours=40, status=EVENT_STATUS_TRADED))
        expired = await repo.expire_stale(max_age_hours=18)
        return expired, (await repo.get_by_id(1)).status

    expired, status = run_with_db(body)
    assert expired == 0
    assert status == EVENT_STATUS_TRADED


def test_expired_events_are_marked_and_stamped():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event("old", age_hours=30))
        await repo.expire_stale(max_age_hours=18)
        return await repo.get_by_id(event.id)

    event = run_with_db(body)
    assert event.status == EVENT_STATUS_EXPIRED
    assert event.processed_at is not None


def test_release_is_a_noop_when_the_queue_is_empty():
    async def body(session):
        return await EventRepository(session).release_queued()

    assert run_with_db(body) == 0


def test_age_hours_reflects_detection_time():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event(age_hours=5))
        return await repo.age_hours(event)

    assert 4.9 < run_with_db(body) < 5.1


def test_missing_sector_falls_back_to_a_shared_bucket():
    assert normalize_sector(None) == SECTOR_UNKNOWN
    assert normalize_sector("") == SECTOR_UNKNOWN
    assert normalize_sector("  ") == SECTOR_UNKNOWN
    assert normalize_sector(" Healthcare ") == "Healthcare"


def test_universe_entry_keeps_the_resolved_sector():
    entry = UniverseEntry(ticker="ACME", sector=normalize_sector("Technology"))
    assert entry.sector == "Technology"
