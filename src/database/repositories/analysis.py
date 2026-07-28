from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow_naive
from src.database.models import Analysis

from .base import BaseRepository


class AnalysisRepository(BaseRepository[Analysis]):

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Analysis)

    async def get_by_event(self, event_id: int) -> Analysis | None:
        result = await self.session.execute(
            select(self.model).where(self.model.event_id == event_id).limit(1)
        )
        return result.scalar_one_or_none()

    async def recent(self, limit: int = 100) -> list[Analysis]:
        result = await self.session.execute(
            select(self.model).order_by(self.model.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def decision_counts(self, days: int = 30) -> dict[str, int]:
        cutoff = utcnow_naive() - timedelta(days=days)
        result = await self.session.execute(
            select(self.model.decision, func.count(self.model.id))
            .where(self.model.created_at >= cutoff)
            .group_by(self.model.decision)
        )
        return {row[0]: row[1] for row in result.all()}

    async def llm_cost_since(self, days: int = 30) -> float:
        cutoff = utcnow_naive() - timedelta(days=days)
        result = await self.session.execute(
            select(func.coalesce(func.sum(self.model.llm_cost_usd), 0)).where(
                self.model.created_at >= cutoff
            )
        )
        return float(result.scalar() or 0)
