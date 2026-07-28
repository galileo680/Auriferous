from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    DRAWDOWN_CAUTION,
    DRAWDOWN_DEFENSIVE,
    DRAWDOWN_HALT,
    DRAWDOWN_NORMAL,
    EquityCurve,
)

from .base import BaseRepository


class EquityRepository(BaseRepository[EquityCurve]):

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, EquityCurve)

    async def get_latest(self) -> EquityCurve | None:
        result = await self.session.execute(
            select(self.model).order_by(self.model.recorded_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_high_water_mark(self, fallback: Decimal) -> Decimal:
        result = await self.session.execute(
            select(self.model.high_water_mark)
            .order_by(self.model.high_water_mark.desc())
            .limit(1)
        )
        value = result.scalar_one_or_none()
        return Decimal(str(value)) if value is not None else fallback

    async def recent(self, limit: int = 200) -> list[EquityCurve]:
        result = await self.session.execute(
            select(self.model).order_by(self.model.recorded_at.desc()).limit(limit)
        )
        return list(reversed(result.scalars().all()))


def classify_drawdown(
    drawdown_pct: float,
    caution: float,
    defensive: float,
    halt: float,
) -> str:
    if drawdown_pct >= halt:
        return DRAWDOWN_HALT
    if drawdown_pct >= defensive:
        return DRAWDOWN_DEFENSIVE
    if drawdown_pct >= caution:
        return DRAWDOWN_CAUTION
    return DRAWDOWN_NORMAL


DRAWDOWN_SIZE_MULTIPLIER = {
    DRAWDOWN_NORMAL: 1.00,
    DRAWDOWN_CAUTION: 0.50,
    DRAWDOWN_DEFENSIVE: 0.25,
    DRAWDOWN_HALT: 0.00,
}

DRAWDOWN_MIN_CONVICTION = {
    DRAWDOWN_NORMAL: 0.00,
    DRAWDOWN_CAUTION: 0.65,
    DRAWDOWN_DEFENSIVE: 0.75,
    DRAWDOWN_HALT: 1.01,
}
