from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    TRADE_STATUS_CLOSED,
    Analysis,
    CalibrationSnapshot,
    ShadowTrade,
    Trade,
)
from src.risk.kelly import conviction_bucket
from src.shadow.book import BOOK_SHADOW


@dataclass(frozen=True)
class CalibrationSample:
    catalyst_type: str
    conviction: float
    pnl_pct: float


class Calibrator:

    def __init__(self) -> None:
        self._logger = structlog.get_logger("Calibrator")

    async def run(self, session: AsyncSession) -> int:
        samples = await self._collect(session)
        if not samples:
            self._logger.info("calibration_no_samples")
            return 0

        buckets: dict[tuple[str, str], list[float]] = {}
        for sample in samples:
            key = (sample.catalyst_type, conviction_bucket(sample.conviction))
            buckets.setdefault(key, []).append(sample.pnl_pct)

        written = 0
        for (catalyst, bucket), returns in sorted(buckets.items()):
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            hit_rate = len(wins) / len(returns)
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.0
            edge = hit_rate * avg_win + (1 - hit_rate) * avg_loss

            session.add(CalibrationSnapshot(
                catalyst_type=catalyst,
                conviction_bucket=bucket,
                sample_size=len(returns),
                hit_rate=Decimal(str(round(hit_rate, 4))),
                avg_win_pct=Decimal(str(round(avg_win * 100, 2))),
                avg_loss_pct=Decimal(str(round(avg_loss * 100, 2))),
                edge=Decimal(str(round(edge, 4))),
            ))
            written += 1
            self._logger.info(
                "calibration_bucket",
                catalyst=catalyst,
                bucket=bucket,
                n=len(returns),
                hit_rate=round(hit_rate, 3),
                edge=round(edge, 4),
            )

        await session.flush()
        return written

    async def _collect(self, session: AsyncSession) -> list[CalibrationSample]:
        samples: list[CalibrationSample] = []

        shadows = await session.execute(
            select(ShadowTrade).where(
                ShadowTrade.book == BOOK_SHADOW,
                ShadowTrade.status == "CLOSED",
                ShadowTrade.catalyst_type.isnot(None),
                ShadowTrade.conviction.isnot(None),
                ShadowTrade.pnl_virtual.isnot(None),
            )
        )
        for row in shadows.scalars().all():
            pct = self._shadow_pct(row)
            if pct is not None:
                samples.append(CalibrationSample(
                    catalyst_type=row.catalyst_type,
                    conviction=float(row.conviction),
                    pnl_pct=pct,
                ))

        trades = await session.execute(
            select(Trade, Analysis)
            .join(Analysis, Analysis.id == Trade.analysis_id)
            .where(
                Trade.status == TRADE_STATUS_CLOSED,
                Trade.pnl_realized.isnot(None),
                Analysis.catalyst_type.isnot(None),
                Analysis.conviction.isnot(None),
            )
        )
        for trade, analysis in trades.all():
            basis = float(trade.capital_at_risk or 0)
            if basis <= 0:
                continue
            samples.append(CalibrationSample(
                catalyst_type=analysis.catalyst_type,
                conviction=float(analysis.conviction),
                pnl_pct=float(trade.pnl_realized) / basis,
            ))

        return samples

    @staticmethod
    def _shadow_pct(row: ShadowTrade) -> Optional[float]:
        basis = float(row.entry_price) * abs(row.quantity)
        if basis <= 0:
            return None
        return float(row.pnl_virtual) / basis
