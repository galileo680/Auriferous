from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow_naive
from src.database.models import (
    TRADE_STATUS_CLOSED,
    TRADE_STATUS_OPEN,
    TRADE_STATUS_PENDING,
    Trade,
)

from .base import BaseRepository

COMMITTED_STATUSES = (TRADE_STATUS_OPEN, TRADE_STATUS_PENDING)


class TradeRepository(BaseRepository[Trade]):

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Trade)

    async def get_committed(self) -> list[Trade]:
        result = await self.session.execute(
            select(self.model)
            .where(self.model.status.in_(COMMITTED_STATUSES))
            .order_by(self.model.opened_at)
        )
        return list(result.scalars().all())

    async def get_by_instrument(self, instrument: str) -> list[Trade]:
        result = await self.session.execute(
            select(self.model).where(
                self.model.status.in_(COMMITTED_STATUSES),
                self.model.instrument == instrument,
            )
        )
        return list(result.scalars().all())

    async def get_tracked_con_ids(self) -> set[int]:
        result = await self.session.execute(
            select(self.model.con_id).where(
                self.model.status.in_(COMMITTED_STATUSES),
                self.model.con_id.isnot(None),
            )
        )
        return {row for row in result.scalars().all() if row is not None}

    async def ticker_is_committed(self, ticker: str) -> bool:
        result = await self.session.execute(
            select(self.model.id).where(
                self.model.ticker == ticker.upper(),
                self.model.status.in_(COMMITTED_STATUSES),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def count_committed(self, instrument: str | None = None) -> int:
        query = select(func.count(self.model.id)).where(
            self.model.status.in_(COMMITTED_STATUSES)
        )
        if instrument is not None:
            query = query.where(self.model.instrument == instrument)
        result = await self.session.execute(query)
        return result.scalar() or 0

    async def total_capital_at_risk(self) -> Decimal:
        result = await self.session.execute(
            select(func.coalesce(func.sum(self.model.capital_at_risk), 0)).where(
                self.model.status.in_(COMMITTED_STATUSES)
            )
        )
        return Decimal(str(result.scalar() or 0))

    async def realized_pnl_total(self) -> Decimal:
        result = await self.session.execute(
            select(func.coalesce(func.sum(self.model.pnl_realized), 0)).where(
                self.model.pnl_realized.isnot(None)
            )
        )
        return Decimal(str(result.scalar() or 0))

    async def close(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        pnl: Decimal | None = None,
    ) -> Trade | None:
        trade = await self.get_by_id(trade_id)
        if trade is None or trade.status not in COMMITTED_STATUSES:
            return None

        price = Decimal(str(round(exit_price, 4)))
        trade.exit_price = price
        trade.exit_reason = exit_reason
        trade.exit_at = utcnow_naive()
        trade.status = TRADE_STATUS_CLOSED

        if pnl is not None:
            trade.pnl_realized = pnl
        elif trade.entry_price is not None:
            multiplier = Decimal(str(trade.contract_spec.get("multiplier") or 1))
            trade.pnl_realized = (
                (price - trade.entry_price) * trade.quantity * multiplier
            ).quantize(Decimal("0.01"))

        await self.session.flush()
        return trade

    async def get_closed(self, limit: int = 500) -> list[Trade]:
        result = await self.session.execute(
            select(self.model)
            .where(self.model.status == TRADE_STATUS_CLOSED)
            .order_by(self.model.exit_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
