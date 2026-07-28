from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.clock import utcnow_naive
from src.core.config import TriageConfig
from src.core.market_clock import NY, MarketClock, MarketState
from src.database.models import (
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_QUEUED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUS_TRIAGED,
    Base,
    Event,
)
from src.database.repositories import EventRepository
from src.triage.agent import render_payload
from src.triage.budget import TriageBudget
from src.triage.context import compute_after_hours_move, compute_change_pct
from src.triage.loop import TriageLoop
from src.triage.models import (
    CATALYST_NOISE,
    MarketContext,
    TriageOutcome,
    TriageResult,
    evaluate_gate,
    normalize_catalyst_type,
)


def make_result(
    actionable: bool = True,
    direction: str = "LONG",
    move: float = 15.0,
    catalyst: str = "FDA_DECISION",
) -> TriageResult:
    return TriageResult(
        is_actionable=actionable,
        catalyst_type=catalyst,
        direction=direction,
        expected_move_pct=move,
        time_to_impact_hours=6,
        reasoning="Binary regulatory decision on the company's lead asset.",
    )


def test_gate_promotes_a_strong_event():
    outcome, _ = evaluate_gate(make_result(), min_expected_move_pct=8.0)
    assert outcome is TriageOutcome.PROMOTED


def test_gate_rejects_not_actionable():
    outcome, reason = evaluate_gate(make_result(actionable=False), 8.0)
    assert outcome is TriageOutcome.REJECTED_NOT_ACTIONABLE
    assert "not actionable" in reason


def test_gate_rejects_unclear_direction():
    outcome, _ = evaluate_gate(make_result(direction="UNCLEAR"), 8.0)
    assert outcome is TriageOutcome.REJECTED_UNCLEAR_DIRECTION


def test_gate_rejects_move_below_threshold():
    outcome, reason = evaluate_gate(make_result(move=5.0), 8.0)
    assert outcome is TriageOutcome.REJECTED_MOVE_TOO_SMALL
    assert "5.0%" in reason


def test_gate_accepts_move_exactly_at_threshold():
    outcome, _ = evaluate_gate(make_result(move=8.0), 8.0)
    assert outcome is TriageOutcome.PROMOTED


def test_gate_accepts_short_direction():
    outcome, _ = evaluate_gate(make_result(direction="SHORT"), 8.0)
    assert outcome is TriageOutcome.PROMOTED


def test_all_rejection_outcomes_are_flagged_as_rejections():
    assert TriageOutcome.REJECTED_NOT_ACTIONABLE.is_rejection
    assert TriageOutcome.REJECTED_LLM_ERROR.is_rejection
    assert not TriageOutcome.PROMOTED.is_rejection
    assert not TriageOutcome.QUEUED.is_rejection


def test_unknown_catalyst_type_falls_back_to_noise():
    assert normalize_catalyst_type("SOMETHING_ELSE") == CATALYST_NOISE
    assert normalize_catalyst_type("") == CATALYST_NOISE
    assert normalize_catalyst_type("fda_decision") == "FDA_DECISION"
    assert normalize_catalyst_type("accounting red flag") == "ACCOUNTING_RED_FLAG"


def test_change_pct_math():
    assert compute_change_pct(110.0, 100.0) == pytest.approx(10.0)
    assert compute_change_pct(90.0, 100.0) == pytest.approx(-10.0)
    assert compute_change_pct(110.0, 0.0) is None


def test_after_hours_move_ignores_noise():
    assert compute_after_hours_move(100.0, 100.001) is None
    assert compute_after_hours_move(100.0, 112.0) == pytest.approx(12.0)
    assert compute_after_hours_move(100.0, None) is None


def test_context_render_marks_missing_fields():
    rendered = MarketContext(ticker="ACME").render()
    assert "unavailable" in rendered
    assert "ACME" not in rendered.split("\n")[0]


def test_context_render_includes_warnings():
    context = MarketContext(ticker="ACME", warnings=["no daily bars returned"])
    assert "no daily bars returned" in context.render()


def test_render_payload_truncates_long_values():
    payload = {"excerpt": "x" * 900, "items": ["2.02", "9.01"]}
    rendered = render_payload(payload)
    assert "[…]" in rendered
    assert "2.02, 9.01" in rendered


def test_render_payload_skips_empty_values():
    rendered = render_payload({"a": None, "b": "", "c": "kept"})
    assert "a:" not in rendered
    assert "c: kept" in rendered


def test_render_payload_handles_empty_dict():
    assert render_payload({}) == "(no structured details)"


class StubAgent:

    def __init__(self, result: TriageResult | None = None, fail: bool = False) -> None:
        self._result = result or make_result()
        self._fail = fail
        self.calls = 0

    async def evaluate(self, event, context) -> TriageResult:
        self.calls += 1
        if self._fail:
            raise RuntimeError("model unavailable")
        return self._result


class StubContextBuilder:

    async def build(self, ticker: str) -> MarketContext:
        return MarketContext(ticker=ticker, last_price=42.0, change_1d_pct=1.5)


class FixedClock(MarketClock):

    def __init__(self, state: MarketState) -> None:
        super().__init__()
        self._state = state

    def state(self, moment=None) -> MarketState:
        return self._state

    def seconds_until_open(self, moment=None) -> float:
        return 3600.0


def make_event(key: str = "k1", age_hours: float = 0.0, priority: int = 2) -> Event:
    return Event(
        source="EDGAR_8K",
        ticker="ACME",
        market="EQUITY",
        payload={"items": ["2.02"], "company": "Acme Biosciences"},
        priority=priority,
        dedup_key=key,
        status="NEW",
        detected_at=utcnow_naive() - timedelta(hours=age_hours),
    )


def run_triage(events: list[Event], agent, state: MarketState, config: TriageConfig | None = None):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as session:
                repo = EventRepository(session)
                for event in events:
                    await repo.create(event)
                await session.commit()

                class FakeDB:
                    def session(self):
                        class Ctx:
                            async def __aenter__(inner):
                                return session

                            async def __aexit__(inner, *args):
                                await session.commit()
                                return False

                        return Ctx()

                loop = TriageLoop(
                    agent=agent,
                    context_builder=StubContextBuilder(),
                    budget=TriageBudget(config or TriageConfig()),
                    config=config or TriageConfig(),
                    clock=FixedClock(state),
                )

                with patch(
                    "src.triage.loop.DatabaseManager.get_instance", return_value=FakeDB()
                ):
                    result = await loop.run()

                statuses: dict[str, str] = {}
                payloads: dict[str, dict] = {}
                for event_id in range(1, len(events) + 1):
                    stored = await repo.get_by_id(event_id)
                    if stored is None:
                        continue
                    statuses[stored.dedup_key] = stored.status
                    payloads[stored.dedup_key] = stored.triage_result

                return result, statuses, payloads
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def test_promoted_event_is_marked_triaged():
    agent = StubAgent(make_result())
    result, statuses, payloads = run_triage([make_event()], agent, MarketState.REGULAR)

    assert result.promoted == 1
    assert result.rejected == 0
    assert statuses["k1"] == EVENT_STATUS_TRIAGED
    assert payloads["k1"]["outcome"] == "PROMOTED"
    assert payloads["k1"]["result"]["catalyst_type"] == "FDA_DECISION"


def test_weak_event_is_rejected_and_not_promoted():
    agent = StubAgent(make_result(move=2.0))
    result, statuses, _ = run_triage([make_event()], agent, MarketState.REGULAR)

    assert result.promoted == 0
    assert result.rejected == 1
    assert statuses["k1"] == EVENT_STATUS_REJECTED


def test_llm_failure_rejects_without_losing_the_event():
    agent = StubAgent(fail=True)
    result, statuses, payloads = run_triage([make_event()], agent, MarketState.REGULAR)

    assert result.errors == 1
    assert statuses["k1"] == EVENT_STATUS_REJECTED
    assert "model call failed" in payloads["k1"]["reason"]


def test_closed_market_queues_without_calling_the_model():
    agent = StubAgent()
    result, statuses, _ = run_triage([make_event()], agent, MarketState.CLOSED)

    assert agent.calls == 0
    assert result.queued == 1
    assert statuses["k1"] == EVENT_STATUS_QUEUED


def test_after_hours_still_analyses_so_the_decision_is_ready():
    agent = StubAgent()
    result, statuses, _ = run_triage([make_event()], agent, MarketState.AFTER_HOURS)

    assert agent.calls == 1
    assert result.promoted == 1
    assert statuses["k1"] == EVENT_STATUS_TRIAGED


def test_stale_event_expires_before_reaching_the_model():
    agent = StubAgent()
    result, statuses, _ = run_triage([make_event(age_hours=30)], agent, MarketState.REGULAR)

    assert agent.calls == 0
    assert result.expired == 1
    assert statuses["k1"] == EVENT_STATUS_EXPIRED


def test_budget_stops_the_model_after_the_hourly_limit():
    agent = StubAgent()
    events = [make_event(f"k{i}") for i in range(1, 6)]
    config = TriageConfig(max_per_hour=2, max_per_day=400)

    result, _, _ = run_triage(events, agent, MarketState.REGULAR, config)

    assert agent.calls == 2
    assert result.examined == 2
    assert result.budget_blocked == 3


def test_higher_priority_events_are_triaged_first():
    agent = StubAgent()
    low = make_event("low", priority=5)
    high = make_event("high", priority=1)
    config = TriageConfig(max_per_hour=1)

    _, statuses, _ = run_triage([low, high], agent, MarketState.REGULAR, config)

    assert statuses["high"] == EVENT_STATUS_TRIAGED
    assert statuses["low"] != EVENT_STATUS_TRIAGED


def test_outcome_counters_are_recorded_per_kind():
    agent = StubAgent(make_result(actionable=False))
    result, _, _ = run_triage([make_event()], agent, MarketState.REGULAR)

    assert result.by_outcome["REJECTED_NOT_ACTIONABLE"] == 1


def test_direction_from_the_model_overwrites_the_filing_hint():
    agent = StubAgent(make_result(direction="SHORT"))
    _, _, payloads = run_triage([make_event()], agent, MarketState.REGULAR)

    assert payloads["k1"]["result"]["direction"] == "SHORT"
