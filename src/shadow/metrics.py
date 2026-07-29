from __future__ import annotations

import math
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    TRADE_STATUS_CLOSED,
    Analysis,
    ShadowTrade,
    Trade,
)
from src.shadow.book import BOOK_PARALLEL, BOOK_SHADOW


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


async def veto_value(session: AsyncSession) -> dict[str, float]:
    rows = await session.execute(
        select(ShadowTrade.origin, func.sum(ShadowTrade.pnl_virtual))
        .where(
            ShadowTrade.book == BOOK_SHADOW,
            ShadowTrade.status == "CLOSED",
            ShadowTrade.pnl_virtual.isnot(None),
        )
        .group_by(ShadowTrade.origin)
    )
    return {origin: float(total or 0) for origin, total in rows.all()}


async def manager_value(session: AsyncSession) -> Optional[float]:
    rows = await session.execute(
        select(Trade.pnl_realized, ShadowTrade.pnl_virtual)
        .join(ShadowTrade, ShadowTrade.trade_id == Trade.id)
        .where(
            Trade.status == TRADE_STATUS_CLOSED,
            Trade.pnl_realized.isnot(None),
            ShadowTrade.book == BOOK_PARALLEL,
            ShadowTrade.status == "CLOSED",
            ShadowTrade.pnl_virtual.isnot(None),
        )
    )
    pairs = rows.all()
    if not pairs:
        return None
    return sum(float(real) - float(virtual) for real, virtual in pairs)


async def triage_precision(
    session: AsyncSession,
    min_move_pct: float,
) -> Optional[float]:
    rows = await session.execute(
        select(ShadowTrade).where(
            ShadowTrade.book == BOOK_SHADOW,
            ShadowTrade.status == "CLOSED",
            ShadowTrade.analysis_id.isnot(None),
            ShadowTrade.pnl_virtual.isnot(None),
        )
    )
    moves = []
    for row in rows.scalars().all():
        basis = float(row.entry_price) * abs(row.quantity)
        if basis > 0:
            moves.append(abs(float(row.pnl_virtual)) / basis * 100)

    if not moves:
        return None
    return sum(1 for move in moves if move >= min_move_pct) / len(moves)


async def priced_in_calibration(session: AsyncSession) -> Optional[float]:
    rows = await session.execute(
        select(ShadowTrade, Analysis)
        .join(Analysis, Analysis.id == ShadowTrade.analysis_id)
        .where(
            ShadowTrade.book == BOOK_SHADOW,
            ShadowTrade.status == "CLOSED",
            ShadowTrade.pnl_virtual.isnot(None),
        )
    )

    scores: list[float] = []
    returns: list[float] = []
    for row, analysis in rows.all():
        verdict = analysis.pricedin_verdict or {}
        score = verdict.get("priced_in_score")
        basis = float(row.entry_price) * abs(row.quantity)
        if score is None or basis <= 0:
            continue
        scores.append(float(score))
        returns.append(float(row.pnl_virtual) / basis)

    return pearson(scores, returns)
