from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.interface import BrokerInterface
from src.core.clock import utcnow_naive
from src.core.config import StructurerConfig
from src.database.models import Analysis, Event
from src.database.session import DatabaseManager
from src.sentinel.models import MARKET_CRYPTO, MARKET_EQUITY
from src.structurer.builder import TradeBuilder
from src.structurer.decision import choose_instrument
from src.structurer.iv import IVAnalyzer
from src.structurer.models import IVProfile, StructureOutcome, StructureResult

BATCH_SIZE = 5
SWARM_DECISION_TRADE = "TRADE"


@dataclass
class StructurerRunResult:
    examined: int = 0
    structured: int = 0
    skipped: int = 0
    errors: int = 0
    by_outcome: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_choice: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class StructurerLoop:

    def __init__(
        self,
        broker: BrokerInterface,
        config: StructurerConfig,
        equity: float,
    ) -> None:
        self._broker = broker
        self._config = config
        self._equity = equity
        self._iv = IVAnalyzer(broker)
        self._logger = structlog.get_logger("StructurerLoop")

    async def run(self) -> StructurerRunResult:
        result = StructurerRunResult()
        db = DatabaseManager.get_instance()

        async with db.session() as session:
            for analysis in await self._pending(session):
                result.examined += 1
                try:
                    outcome = await self._structure_one(session, analysis)
                except Exception as e:
                    self._logger.error(
                        "structurer_failed",
                        analysis_id=analysis.id,
                        ticker=analysis.ticker,
                        error=str(e),
                    )
                    outcome = StructureResult(
                        outcome=StructureOutcome.SKIP_ERROR,
                        reason=f"structuring failed: {e}",
                    )
                    result.errors += 1

                analysis.structure_result = outcome.to_payload()
                analysis.structured_at = utcnow_naive()

                result.by_outcome[outcome.outcome.value] += 1
                if outcome.structured:
                    result.structured += 1
                    result.by_choice[outcome.trade.choice.value] += 1
                elif outcome.outcome is not StructureOutcome.SKIP_ERROR:
                    result.skipped += 1

        if result.examined:
            self._logger.info(
                "structurer_cycle",
                examined=result.examined,
                structured=result.structured,
                skipped=result.skipped,
                errors=result.errors,
                by_outcome=dict(result.by_outcome),
                by_choice=dict(result.by_choice),
            )
        return result

    async def _pending(self, session: AsyncSession) -> list[Analysis]:
        result = await session.execute(
            select(Analysis)
            .where(
                Analysis.decision == SWARM_DECISION_TRADE,
                Analysis.structured_at.is_(None),
            )
            .order_by(Analysis.created_at)
            .limit(BATCH_SIZE)
        )
        return list(result.scalars().all())

    async def _structure_one(
        self,
        session: AsyncSession,
        analysis: Analysis,
    ) -> StructureResult:
        event = await session.get(Event, analysis.event_id)
        market = event.market if event else MARKET_EQUITY

        iv = (
            IVProfile(source="crypto — IV rank not applicable")
            if market == MARKET_CRYPTO
            else await self._iv.profile(analysis.ticker)
        )

        underlying_price = await self._peek_price(analysis.ticker, market)
        conviction = float(analysis.conviction or 0)
        horizon = int(analysis.horizon_days or self._config.binary_event_max_horizon_days)

        decision = choose_instrument(
            market=market,
            direction=analysis.direction or "LONG",
            conviction=conviction,
            horizon_days=horizon,
            catalyst_type=analysis.catalyst_type,
            iv_rank=iv.iv_rank,
            underlying_price=underlying_price,
            config=self._config,
        )

        self._logger.info(
            "instrument_chosen",
            ticker=analysis.ticker,
            choice=decision.choice.value,
            binary_event=decision.binary_event,
            iv_rank=round(iv.iv_rank, 1) if iv.iv_rank is not None else None,
            conviction=round(conviction, 2),
            reason=decision.reason,
        )

        builder = TradeBuilder(self._broker, self._config, self._equity)
        return await builder.build(
            ticker=analysis.ticker,
            market=market,
            direction=analysis.direction or "LONG",
            decision=decision,
            conviction=conviction,
            horizon_days=horizon,
            target_move_pct=float(analysis.expected_move_pct or 0),
            iv=iv,
            event_date=self._event_date(horizon),
            invalidation=self._invalidation(analysis),
            catalyst_type=analysis.catalyst_type,
        )

    async def _peek_price(self, ticker: str, market: str) -> float | None:
        if market == MARKET_CRYPTO:
            return None
        try:
            from src.broker.contracts import stock_spec

            quote = await self._broker.get_quote(stock_spec(ticker))
            return quote.last if quote.last > 0 else None
        except Exception:
            return None

    @staticmethod
    def _event_date(horizon_days: int) -> date:
        return utcnow_naive().date() + timedelta(days=min(horizon_days, 21))

    @staticmethod
    def _invalidation(analysis: Analysis) -> list[str]:
        criteria: list[str] = []

        bull = analysis.bull_thesis or {}
        bear = analysis.bear_thesis or {}
        redteam = analysis.redteam_verdict or {}

        winner = bull if (analysis.direction or "LONG") == "LONG" else bear
        assumption = winner.get("key_assumption")
        if assumption:
            criteria.append(f"key assumption breaks: {assumption}")

        kill = redteam.get("strongest_kill_argument")
        if kill:
            criteria.append(f"red team scenario materialises: {kill}")

        return criteria or ["thesis no longer supported by new information"]
