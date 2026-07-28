from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from src.core.config import TriageConfig
from src.core.market_clock import MarketClock, MarketState
from src.database.models import (
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_QUEUED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUS_TRIAGED,
    Event,
)
from src.database.repositories import EventRepository
from src.database.session import DatabaseManager
from src.sentinel.gate import GateDecision, decide
from src.triage.agent import TriageAgent
from src.triage.budget import TriageBudget
from src.triage.context import ContextBuilder
from src.triage.models import TriageDecision, TriageOutcome, evaluate_gate

BATCH_SIZE = 25


@dataclass
class TriageRunResult:
    examined: int = 0
    promoted: int = 0
    rejected: int = 0
    queued: int = 0
    expired: int = 0
    budget_blocked: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    by_outcome: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, outcome: TriageOutcome) -> None:
        self.by_outcome[outcome.value] += 1


class TriageLoop:

    def __init__(
        self,
        agent: TriageAgent,
        context_builder: ContextBuilder,
        budget: TriageBudget,
        config: TriageConfig,
        clock: MarketClock | None = None,
    ) -> None:
        self._agent = agent
        self._context = context_builder
        self._budget = budget
        self._config = config
        self._clock = clock or MarketClock()
        self._logger = structlog.get_logger("TriageLoop")

    async def run(self) -> TriageRunResult:
        result = TriageRunResult()
        db = DatabaseManager.get_instance()
        state = self._clock.state()

        async with db.session() as session:
            repo = EventRepository(session)

            result.expired += await repo.expire_stale(max_age_hours=18)

            if state is MarketState.REGULAR:
                released = await repo.release_queued()
                if released:
                    self._logger.info("queued_events_released", count=released)

            if not state.can_analyze:
                pending = await repo.get_pending(limit=BATCH_SIZE)
                for event in pending:
                    await repo.queue_for_open(event.id)
                    result.queued += 1
                    result.record(TriageOutcome.QUEUED)
                if result.queued:
                    self._logger.info(
                        "events_queued_market_closed",
                        count=result.queued,
                        next_open_in_hours=round(self._clock.seconds_until_open() / 3600, 1),
                    )
                return result

            budget = await self._budget.status(session)
            pending = await repo.get_pending(limit=BATCH_SIZE)

            for event in pending:
                if result.examined >= budget.allowed:
                    result.budget_blocked += 1
                    result.record(TriageOutcome.BUDGET_EXHAUSTED)
                    await self._persist(
                        session, repo, event,
                        TriageDecision(TriageOutcome.BUDGET_EXHAUSTED, reason=budget.reason()),
                    )
                    continue

                verdict = decide(state, await repo.age_hours(event))

                if verdict.decision is GateDecision.EXPIRE:
                    await self._persist(
                        session, repo, event,
                        TriageDecision(TriageOutcome.EXPIRED, reason=verdict.reason),
                    )
                    result.expired += 1
                    result.record(TriageOutcome.EXPIRED)
                    continue

                if verdict.decision is GateDecision.QUEUE_FOR_OPEN:
                    await repo.queue_for_open(event.id)
                    result.queued += 1
                    result.record(TriageOutcome.QUEUED)
                    continue

                decision = await self._triage_one(event)
                result.examined += 1
                result.cost_usd += decision.cost_usd
                result.record(decision.outcome)

                if decision.promoted:
                    result.promoted += 1
                elif decision.outcome is TriageOutcome.REJECTED_LLM_ERROR:
                    result.errors += 1
                    result.rejected += 1
                else:
                    result.rejected += 1

                await self._persist(session, repo, event, decision)

        if result.examined or result.queued or result.expired:
            self._logger.info(
                "triage_cycle",
                market_state=state.value,
                examined=result.examined,
                promoted=result.promoted,
                rejected=result.rejected,
                queued=result.queued,
                expired=result.expired,
                budget_blocked=result.budget_blocked,
                errors=result.errors,
                by_outcome=dict(result.by_outcome),
            )
        return result

    async def _triage_one(self, event: Event) -> TriageDecision:
        try:
            context = await self._context.build(event.ticker)
        except Exception as e:
            self._logger.warning(
                "context_build_failed", ticker=event.ticker, error=str(e)
            )
            from src.triage.models import MarketContext

            context = MarketContext(ticker=event.ticker, warnings=["context unavailable"])

        try:
            result = await self._agent.evaluate(event, context)
        except Exception as e:
            self._logger.error(
                "triage_llm_failed", ticker=event.ticker, event_id=event.id, error=str(e)
            )
            return TriageDecision(
                outcome=TriageOutcome.REJECTED_LLM_ERROR,
                context=context,
                reason=f"model call failed: {e}",
            )

        outcome, reason = evaluate_gate(result, self._config.min_expected_move_pct)

        self._logger.info(
            "triage_decision",
            ticker=event.ticker,
            source=event.source,
            outcome=outcome.value,
            catalyst_type=result.catalyst_type,
            direction=result.direction,
            expected_move_pct=round(result.expected_move_pct, 1),
            reasoning=result.reasoning[:160],
        )

        return TriageDecision(
            outcome=outcome,
            result=result,
            context=context,
            reason=reason,
        )

    async def _persist(
        self,
        session,
        repo: EventRepository,
        event: Event,
        decision: TriageDecision,
    ) -> None:
        event.triage_result = decision.to_payload()

        if decision.result is not None:
            event.catalyst_type = decision.result.catalyst_type
            if decision.result.direction in ("LONG", "SHORT"):
                event.direction = decision.result.direction

        if decision.outcome is TriageOutcome.PROMOTED:
            status = EVENT_STATUS_TRIAGED
        elif decision.outcome is TriageOutcome.EXPIRED:
            status = EVENT_STATUS_EXPIRED
        elif decision.outcome is TriageOutcome.QUEUED:
            status = EVENT_STATUS_QUEUED
        else:
            status = EVENT_STATUS_REJECTED

        await repo.mark(event.id, status)
