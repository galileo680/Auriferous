from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.clock import utcnow_naive
from src.core.config import SwarmConfig
from src.database.models import (
    EVENT_STATUS_ANALYZED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUS_TRIAGED,
    Analysis,
    Base,
    Event,
)
from src.database.repositories import AnalysisRepository, EventRepository
from src.swarm.agents import render_measurements
from src.swarm.evidence import EvidenceBundle, Fundamentals, PriceSnapshot
from src.swarm.loop import SwarmLoop
from src.swarm.models import (
    AgentCost,
    PricedInVerdict,
    RedTeamVerdict,
    SwarmOutcome,
    Thesis,
    compute_conviction,
    synthesize,
)

CONFIG = SwarmConfig()


def thesis(stance: str = "BULL", confidence: float = 0.8, horizon: int = 14) -> Thesis:
    return Thesis(
        stance=stance,
        core_argument="The contract is a fifth of revenue and was not in guidance.",
        supporting_evidence=["8-K item 1.01", "revenue TTM $240M"],
        expected_move_pct=18.0,
        time_horizon_days=horizon,
        confidence=confidence,
        key_assumption="The contract is incremental, not a renewal.",
    )


def redteam(kill: float = 0.2, fatal: bool = False) -> RedTeamVerdict:
    return RedTeamVerdict(
        strongest_kill_argument="The shelf registration allows immediate dilution.",
        kill_confidence=kill,
        assumption_attacked="The contract is incremental.",
        evidence=["S-3 filed six weeks ago"],
        fatal=fatal,
    )


def pricedin(score: float = 0.3, remaining: float = 12.0) -> PricedInVerdict:
    return PricedInVerdict(
        priced_in_score=score,
        remaining_move_pct=remaining,
        crowding_risk="LOW",
        reasoning="Volume is only 1.4x and the stock has not gapped.",
    )


def run_synth(bull=None, bear=None, red=None, priced=None):
    return synthesize(
        bull=bull or thesis("BULL", 0.85),
        bear=bear or thesis("BEAR", 0.25),
        redteam=red or redteam(),
        pricedin=priced or pricedin(),
        redteam_kill_threshold=CONFIG.redteam_kill_threshold,
        pricedin_veto_threshold=CONFIG.pricedin_veto_threshold,
        min_conviction=CONFIG.min_conviction,
    )


def test_clean_setup_produces_a_trade():
    verdict = run_synth()
    assert verdict.outcome is SwarmOutcome.TRADE
    assert verdict.direction == "LONG"
    assert verdict.expected_move_pct == pytest.approx(12.0)
    assert verdict.time_horizon_days == 14


def test_bear_side_wins_when_more_confident():
    verdict = run_synth(bull=thesis("BULL", 0.3), bear=thesis("BEAR", 0.8))
    assert verdict.direction == "SHORT"


def test_fatal_redteam_verdict_vetoes_regardless_of_confidence():
    verdict = run_synth(red=redteam(kill=0.1, fatal=True))
    assert verdict.outcome is SwarmOutcome.VETO_REDTEAM
    assert "fatal" in verdict.veto_reason


def test_redteam_above_threshold_vetoes():
    verdict = run_synth(red=redteam(kill=0.75))
    assert verdict.outcome is SwarmOutcome.VETO_REDTEAM


def test_redteam_exactly_at_threshold_does_not_veto():
    verdict = run_synth(red=redteam(kill=0.70))
    assert verdict.outcome is SwarmOutcome.TRADE


def test_priced_in_above_threshold_vetoes():
    verdict = run_synth(priced=pricedin(score=0.9, remaining=1.0))
    assert verdict.outcome is SwarmOutcome.VETO_PRICEDIN
    assert "0.90" in verdict.veto_reason


def test_redteam_veto_takes_precedence_over_priced_in():
    verdict = run_synth(red=redteam(kill=0.9), priced=pricedin(score=0.99))
    assert verdict.outcome is SwarmOutcome.VETO_REDTEAM


def test_two_weak_sides_are_vetoed_for_low_conviction():
    verdict = run_synth(bull=thesis("BULL", 0.4), bear=thesis("BEAR", 0.35))
    assert verdict.outcome is SwarmOutcome.VETO_LOW_CONVICTION


def test_one_strong_side_survives_the_conviction_gate():
    verdict = run_synth(bull=thesis("BULL", 0.6), bear=thesis("BEAR", 0.2))
    assert verdict.outcome is SwarmOutcome.TRADE


def test_conviction_discounts_for_kill_and_priced_in():
    assert compute_conviction(0.9, 0.1, 0.0, 0.0) == pytest.approx(0.8)
    assert compute_conviction(0.9, 0.1, 0.5, 0.0) == pytest.approx(0.4)
    assert compute_conviction(0.9, 0.1, 0.0, 0.5) == pytest.approx(0.4)
    assert compute_conviction(0.9, 0.1, 0.5, 0.5) == pytest.approx(0.2)


def test_conviction_is_low_when_both_sides_are_confident():
    assert compute_conviction(0.9, 0.85, 0.0, 0.0) == pytest.approx(0.05)


def test_conviction_stays_within_bounds():
    assert compute_conviction(1.0, 0.0, 0.0, 0.0) == 1.0
    assert compute_conviction(0.5, 0.5, 0.0, 0.0) == 0.0


def test_expected_move_comes_from_priced_in_not_from_the_thesis():
    verdict = run_synth(priced=pricedin(score=0.4, remaining=6.0))
    assert verdict.expected_move_pct == pytest.approx(6.0)


def test_veto_outcomes_are_flagged():
    assert SwarmOutcome.VETO_REDTEAM.is_veto
    assert SwarmOutcome.VETO_AGENT_ERROR.is_veto
    assert not SwarmOutcome.TRADE.is_veto


def test_agent_cost_merges():
    total = AgentCost()
    total.merge(AgentCost(calls=1, input_tokens=100, output_tokens=50, usd=0.01))
    total.merge(AgentCost(calls=1, input_tokens=200, output_tokens=80, usd=0.02))
    assert total.calls == 2
    assert total.input_tokens == 300
    assert total.usd == pytest.approx(0.03)


def test_evidence_render_marks_missing_sections():
    bundle = EvidenceBundle(ticker="ACME", event_summary="- ticker: ACME", triage_summary="- none")
    rendered = bundle.render()
    assert "price history unavailable" in rendered
    assert "FUNDAMENTALS" in rendered


def test_evidence_render_includes_extended_hours_when_present():
    bundle = EvidenceBundle(
        ticker="ACME",
        event_summary="e",
        triage_summary="t",
        after_hours_move_pct=7.5,
    )
    assert "+7.50%" in bundle.render()


def test_price_snapshot_renders_all_windows():
    snapshot = PriceSnapshot(
        last=42.0,
        changes_pct={1: 5.0, 5: -3.0, 20: 12.0},
        atr_pct=4.2,
        range_52w_position=0.85,
        volume_ratio=3.1,
        gap_at_open_pct=2.5,
    )
    rendered = snapshot.render()
    assert "+5.00%" in rendered
    assert "-3.00%" in rendered
    assert "85%" in rendered


def test_priced_in_inputs_expose_unavailable_iv():
    bundle = EvidenceBundle(ticker="ACME", event_summary="e", triage_summary="t")
    inputs = bundle.priced_in_inputs()
    assert inputs["iv_rank"] is None
    assert "iv_vs_realized" in inputs


def test_render_measurements_labels_missing_values():
    rendered = render_measurements({"iv_rank": None, "move_1d_pct": 6.5})
    assert "iv rank: not available" in rendered
    assert "move 1d pct: +6.50" in rendered


def test_fundamentals_flag_dilution_risk_for_prerevenue():
    fundamentals = Fundamentals(total_cash=200_000_000, revenue_ttm=1_000_000)
    assert "dilution risk is material" in fundamentals.render()


class StubAgents:

    def __init__(self, verdicts=None, fail: bool = False) -> None:
        self._verdicts = verdicts or (thesis("BULL", 0.85), thesis("BEAR", 0.2), redteam(), pricedin())
        self._fail = fail
        self.calls = 0

    async def run_all(self, evidence):
        self.calls += 1
        if self._fail:
            raise RuntimeError("model unavailable")
        bull, bear, red, priced = self._verdicts
        return bull, bear, red, priced, AgentCost(calls=4, input_tokens=4000, output_tokens=800, usd=0.013)


class StubCollector:

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def collect(self, event) -> EvidenceBundle:
        if self._fail:
            raise RuntimeError("yfinance down")
        return EvidenceBundle(ticker=event.ticker, event_summary="e", triage_summary="t")


def make_event(key: str = "k1", priority: int = 2) -> Event:
    return Event(
        source="EDGAR_8K",
        ticker="ACME",
        market="EQUITY",
        payload={"items": ["1.01"]},
        priority=priority,
        dedup_key=key,
        status=EVENT_STATUS_TRIAGED,
        triage_result={"result": {"catalyst_type": "MA", "direction": "LONG"}},
        detected_at=utcnow_naive(),
    )


def run_swarm(events, agents, collector=None, config=None, seed_analyses=0):
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

                for index in range(seed_analyses):
                    spent = await repo.create(Event(
                        source="EDGAR_8K",
                        ticker=f"OLD{index}",
                        market="EQUITY",
                        priority=3,
                        dedup_key=f"seed-{index}",
                        status=EVENT_STATUS_REJECTED,
                        detected_at=utcnow_naive() - timedelta(hours=2),
                    ))
                    await AnalysisRepository(session).create(Analysis(
                        event_id=spent.id,
                        ticker=spent.ticker,
                        decision="TRADE",
                        llm_cost_usd=1.0,
                    ))
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

                loop = SwarmLoop(
                    agents=agents,
                    collector=collector or StubCollector(),
                    config=config or SwarmConfig(),
                )

                with patch(
                    "src.swarm.loop.DatabaseManager.get_instance", return_value=FakeDB()
                ):
                    result = await loop.run()

                statuses = {}
                for event_id in range(1, len(events) + 1):
                    stored = await repo.get_by_id(event_id)
                    if stored is not None:
                        statuses[stored.dedup_key] = stored.status

                analyses = await AnalysisRepository(session).recent(limit=50)
                return result, statuses, analyses
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def test_tradeable_verdict_marks_event_analyzed_and_writes_analysis():
    result, statuses, analyses = run_swarm([make_event()], StubAgents())

    assert result.trades == 1
    assert statuses["k1"] == EVENT_STATUS_ANALYZED
    written = [a for a in analyses if a.ticker == "ACME"][0]
    assert written.decision == "TRADE"
    assert written.direction == "LONG"
    assert float(written.llm_cost_usd) == pytest.approx(0.013)


def test_vetoed_verdict_marks_event_rejected():
    agents = StubAgents((thesis("BULL", 0.8), thesis("BEAR", 0.2), redteam(kill=0.9), pricedin()))
    result, statuses, analyses = run_swarm([make_event()], agents)

    assert result.vetoes == 1
    assert result.trades == 0
    assert statuses["k1"] == EVENT_STATUS_REJECTED
    assert [a for a in analyses if a.ticker == "ACME"][0].decision == "VETO_REDTEAM"


def test_agent_failure_is_recorded_without_losing_the_event():
    result, statuses, analyses = run_swarm([make_event()], StubAgents(fail=True))

    assert result.errors == 1
    assert statuses["k1"] == EVENT_STATUS_REJECTED
    assert "agent call failed" in [a for a in analyses if a.ticker == "ACME"][0].veto_reason


def test_evidence_failure_skips_the_model_entirely():
    agents = StubAgents()
    result, _, analyses = run_swarm([make_event()], agents, collector=StubCollector(fail=True))

    assert agents.calls == 0
    assert result.errors == 1
    assert "evidence collection failed" in [a for a in analyses if a.ticker == "ACME"][0].veto_reason


def test_daily_count_budget_blocks_further_analysis():
    agents = StubAgents()
    events = [make_event(f"k{i}") for i in range(1, 4)]
    config = SwarmConfig(max_per_day=1)

    result, _, _ = run_swarm(events, agents, config=config)

    assert agents.calls == 1
    assert result.analysed == 1
    assert result.budget_blocked == 2


def test_cost_budget_blocks_when_already_spent():
    agents = StubAgents()
    config = SwarmConfig(max_cost_usd_per_day=2.0)

    result, _, _ = run_swarm([make_event()], agents, config=config, seed_analyses=3)

    assert agents.calls == 0
    assert result.budget_blocked == 1


def test_higher_priority_events_are_analysed_first():
    agents = StubAgents()
    events = [make_event("low", priority=5), make_event("high", priority=1)]
    config = SwarmConfig(max_per_day=1)

    _, statuses, _ = run_swarm(events, agents, config=config)

    assert statuses["high"] == EVENT_STATUS_ANALYZED
    assert statuses["low"] == EVENT_STATUS_REJECTED
