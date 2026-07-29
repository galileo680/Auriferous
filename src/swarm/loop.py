from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow_naive
from src.core.config import SwarmConfig
from src.database.models import (
    EVENT_STATUS_ANALYZED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUS_TRIAGED,
    Analysis,
    Event,
)
from src.database.repositories import AnalysisRepository, EventRepository
from src.database.session import DatabaseManager
from src.swarm.agents import SwarmAgents
from src.swarm.evidence import EvidenceCollector
from src.swarm.models import SwarmOutcome, SwarmVerdict, synthesize

BATCH_SIZE = 5


@dataclass
class SwarmBudgetStatus:
    allowed: int
    used_today: int
    spent_usd: float
    daily_limit: int
    cost_limit_usd: float

    @property
    def exhausted(self) -> bool:
        return self.allowed <= 0

    def reason(self) -> str:
        if self.spent_usd >= self.cost_limit_usd:
            return f"daily swarm cost limit reached (${self.spent_usd:.2f}/${self.cost_limit_usd:.2f})"
        return f"daily swarm count limit reached ({self.used_today}/{self.daily_limit})"


@dataclass
class SwarmRunResult:
    analysed: int = 0
    trades: int = 0
    vetoes: int = 0
    errors: int = 0
    budget_blocked: int = 0
    cost_usd: float = 0.0
    by_outcome: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class SwarmLoop:

    def __init__(
        self,
        agents: SwarmAgents,
        collector: EvidenceCollector,
        config: SwarmConfig,
    ) -> None:
        self._agents = agents
        self._collector = collector
        self._config = config
        self._logger = structlog.get_logger("SwarmLoop")

    async def run(self) -> SwarmRunResult:
        result = SwarmRunResult()
        db = DatabaseManager.get_instance()

        async with db.session() as session:
            budget = await self._budget_status(session)
            pending = await self._pending_events(session)

            for event in pending:
                if result.analysed >= budget.allowed:
                    result.budget_blocked += 1
                    result.by_outcome[SwarmOutcome.BUDGET_EXHAUSTED.value] += 1
                    await self._persist(
                        session,
                        event,
                        SwarmVerdict(
                            outcome=SwarmOutcome.BUDGET_EXHAUSTED,
                            veto_reason=budget.reason(),
                        ),
                    )
                    continue

                verdict = await self._analyse(event)
                result.analysed += 1
                result.cost_usd += verdict.cost.usd
                result.by_outcome[verdict.outcome.value] += 1

                if verdict.tradeable:
                    result.trades += 1
                elif verdict.outcome is SwarmOutcome.VETO_AGENT_ERROR:
                    result.errors += 1
                    result.vetoes += 1
                else:
                    result.vetoes += 1

                await self._persist(session, event, verdict)

        if result.analysed or result.budget_blocked:
            self._logger.info(
                "swarm_cycle",
                analysed=result.analysed,
                trades=result.trades,
                vetoes=result.vetoes,
                errors=result.errors,
                budget_blocked=result.budget_blocked,
                cost_usd=round(result.cost_usd, 4),
                by_outcome=dict(result.by_outcome),
            )
        return result

    async def _pending_events(self, session: AsyncSession) -> list[Event]:
        result = await session.execute(
            select(Event)
            .where(Event.status == EVENT_STATUS_TRIAGED)
            .order_by(Event.priority, Event.detected_at)
            .limit(BATCH_SIZE)
        )
        return list(result.scalars().all())

    async def _budget_status(self, session: AsyncSession) -> SwarmBudgetStatus:
        cutoff = utcnow_naive() - timedelta(hours=24)

        counted = await session.execute(
            select(func.count(Analysis.id)).where(Analysis.created_at >= cutoff)
        )
        used_today = counted.scalar() or 0

        spent = await session.execute(
            select(func.coalesce(func.sum(Analysis.llm_cost_usd), 0)).where(
                Analysis.created_at >= cutoff
            )
        )
        spent_usd = float(spent.scalar() or 0)

        by_count = self._config.max_per_day - used_today
        by_cost = BATCH_SIZE if spent_usd < self._config.max_cost_usd_per_day else 0

        return SwarmBudgetStatus(
            allowed=max(min(by_count, by_cost), 0),
            used_today=used_today,
            spent_usd=spent_usd,
            daily_limit=self._config.max_per_day,
            cost_limit_usd=self._config.max_cost_usd_per_day,
        )

    async def _analyse(self, event: Event) -> SwarmVerdict:
        try:
            evidence = await self._collector.collect(event)
        except Exception as e:
            self._logger.error(
                "evidence_collection_failed", ticker=event.ticker, error=str(e)
            )
            return SwarmVerdict(
                outcome=SwarmOutcome.VETO_AGENT_ERROR,
                veto_reason=f"evidence collection failed: {e}",
            )

        try:
            bull, bear, redteam, pricedin, cost = await self._agents.run_all(evidence)
        except Exception as e:
            self._logger.error("swarm_agents_failed", ticker=event.ticker, error=str(e))
            return SwarmVerdict(
                outcome=SwarmOutcome.VETO_AGENT_ERROR,
                veto_reason=f"agent call failed: {e}",
            )

        verdict = synthesize(
            bull=bull,
            bear=bear,
            redteam=redteam,
            pricedin=pricedin,
            redteam_kill_threshold=self._config.redteam_kill_threshold,
            pricedin_veto_threshold=self._config.pricedin_veto_threshold,
            min_conviction=self._config.min_conviction,
        )
        verdict.cost = cost
        verdict.warnings = list(evidence.warnings)

        self._logger.info(
            "swarm_verdict",
            ticker=event.ticker,
            outcome=verdict.outcome.value,
            direction=verdict.direction,
            conviction=round(verdict.conviction, 3),
            remaining_move_pct=round(verdict.expected_move_pct, 1),
            bull_confidence=round(bull.confidence, 2),
            bear_confidence=round(bear.confidence, 2),
            kill_confidence=round(redteam.kill_confidence, 2),
            priced_in=round(pricedin.priced_in_score, 2),
            cost_usd=round(cost.usd, 4),
        )
        return verdict

    async def _persist(
        self,
        session: AsyncSession,
        event: Event,
        verdict: SwarmVerdict,
    ) -> None:
        analysis = Analysis(
            event_id=event.id,
            ticker=event.ticker,
            bull_thesis=verdict.bull.model_dump() if verdict.bull else None,
            bear_thesis=verdict.bear.model_dump() if verdict.bear else None,
            redteam_verdict=verdict.redteam.model_dump() if verdict.redteam else None,
            pricedin_verdict=verdict.pricedin.model_dump() if verdict.pricedin else None,
            direction=verdict.direction,
            conviction=Decimal(str(round(verdict.conviction, 4))),
            expected_move_pct=Decimal(str(round(verdict.expected_move_pct, 2))),
            decision=verdict.outcome.value,
            veto_reason=verdict.veto_reason,
            llm_cost_usd=Decimal(str(round(verdict.cost.usd, 4))),
            horizon_days=verdict.time_horizon_days or None,
            catalyst_type=event.catalyst_type,
        )
        await AnalysisRepository(session).create(analysis)

        status = EVENT_STATUS_ANALYZED if verdict.tradeable else EVENT_STATUS_REJECTED
        await EventRepository(session).mark(event.id, status)
