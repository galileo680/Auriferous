from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow_naive
from src.database.models import EVENT_STATUS_NEW, Event

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

    async def count_since(self, hours: int) -> int:
        cutoff = utcnow_naive() - timedelta(hours=hours)
        result = await self.session.execute(
            select(self.model.id).where(self.model.detected_at >= cutoff)
        )
        return len(result.scalars().all())
