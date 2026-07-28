from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow_naive
from src.database.models import (
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_NEW,
    EVENT_STATUS_PENDING,
    EVENT_STATUS_QUEUED,
    Event,
)

from .base import BaseRepository


class EventRepository(BaseRepository[Event]):

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Event)

    async def exists_dedup_key(self, dedup_key: str, window_hours: int = 24) -> bool:
        cutoff = utcnow_naive() - timedelta(hours=window_hours)
        result = await self.session.execute(
            select(self.model.id).where(
                self.model.dedup_key == dedup_key,
                self.model.detected_at >= cutoff,
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_pending(self, limit: int = 50) -> list[Event]:
        result = await self.session.execute(
            select(self.model)
            .where(self.model.status == EVENT_STATUS_NEW)
            .order_by(self.model.priority, self.model.detected_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark(self, event_id: int, status: str) -> bool:
        event = await self.get_by_id(event_id)
        if event is None:
            return False
        event.status = status
        event.processed_at = utcnow_naive()
        await self.session.flush()
        return True

    async def get_queued(self, limit: int = 100) -> list[Event]:
        result = await self.session.execute(
            select(self.model)
            .where(self.model.status == EVENT_STATUS_QUEUED)
            .order_by(self.model.priority, self.model.detected_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def queue_for_open(self, event_id: int) -> bool:
        return await self.mark(event_id, EVENT_STATUS_QUEUED)

    async def release_queued(self) -> int:
        released = 0
        for event in await self.get_queued(limit=500):
            event.status = EVENT_STATUS_NEW
            event.processed_at = None
            released += 1

        if released:
            await self.session.flush()
            self.logger.info("events_released_at_open", count=released)
        return released

    async def expire_stale(self, max_age_hours: int = 18) -> int:
        cutoff = utcnow_naive() - timedelta(hours=max_age_hours)
        result = await self.session.execute(
            select(self.model).where(
                self.model.status.in_(EVENT_STATUS_PENDING),
                self.model.detected_at < cutoff,
            )
        )

        expired = 0
        for event in result.scalars().all():
            event.status = EVENT_STATUS_EXPIRED
            event.processed_at = utcnow_naive()
            expired += 1

        if expired:
            await self.session.flush()
            self.logger.info(
                "stale_events_expired", count=expired, max_age_hours=max_age_hours
            )
        return expired

    async def age_hours(self, event: Event) -> float:
        if event.detected_at is None:
            return 0.0
        return (utcnow_naive() - event.detected_at).total_seconds() / 3600

    async def count_since(self, hours: int) -> int:
        cutoff = utcnow_naive() - timedelta(hours=hours)
        result = await self.session.execute(
            select(self.model.id).where(self.model.detected_at >= cutoff)
        )
        return len(result.scalars().all())
