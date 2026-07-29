from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow_naive
from src.database.models import Analysis
from src.database.session import DatabaseManager
from src.risk.governor import RiskGovernor
from src.structurer.models import StructureOutcome

BATCH_SIZE = 10
SWARM_DECISION_TRADE = "TRADE"


@dataclass
class GovernorRunResult:
    examined: int = 0
    approved: int = 0
    vetoed: int = 0
    unstructured: int = 0
    by_reason: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class GovernorLoop:

    def __init__(self, governor: RiskGovernor) -> None:
        self._governor = governor
        self._logger = structlog.get_logger("GovernorLoop")

    async def run(self) -> GovernorRunResult:
        result = GovernorRunResult()
        db = DatabaseManager.get_instance()

        async with db.session() as session:
            pending = await self._pending(session)
            if not pending:
                return result

            snapshot = await self._governor.tracker.snapshot(session)

            for analysis in pending:
                result.examined += 1
                outcome = (analysis.structure_result or {}).get("outcome")

                if outcome != StructureOutcome.STRUCTURED.value:
                    analysis.governed_at = utcnow_naive()
                    result.unstructured += 1
                    continue

                verdict = await self._governor.assess(session, analysis, snapshot)
                analysis.risk_verdict = verdict.to_payload()
                analysis.governed_at = utcnow_naive()

                if verdict.approved:
                    result.approved += 1
                else:
                    result.vetoed += 1
                    result.by_reason[verdict.veto_reason or "unknown"] += 1

                self._logger.info(
                    "risk_verdict",
                    ticker=analysis.ticker,
                    approved=verdict.approved,
                    quantity=verdict.quantity,
                    capital_at_risk=round(verdict.capital_at_risk, 2),
                    kelly_fraction=round(verdict.kelly_fraction_used, 4),
                    drawdown_state=verdict.drawdown_state,
                    hit_rate=round(verdict.hit_rate_used, 3),
                    payoff_odds=round(verdict.payoff_odds_used, 2),
                    veto_reason=verdict.veto_reason,
                )

        if result.examined:
            self._logger.info(
                "governor_cycle",
                examined=result.examined,
                approved=result.approved,
                vetoed=result.vetoed,
                unstructured=result.unstructured,
                by_reason=dict(result.by_reason),
            )
        return result

    async def _pending(self, session: AsyncSession) -> list[Analysis]:
        result = await session.execute(
            select(Analysis)
            .where(
                Analysis.decision == SWARM_DECISION_TRADE,
                Analysis.structured_at.isnot(None),
                Analysis.governed_at.is_(None),
            )
            .order_by(Analysis.structured_at)
            .limit(BATCH_SIZE)
        )
        return list(result.scalars().all())
