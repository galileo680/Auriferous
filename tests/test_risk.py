from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import CapitalConfig, RiskConfig
from src.database.models import (
    DRAWDOWN_CAUTION,
    DRAWDOWN_DEFENSIVE,
    DRAWDOWN_HALT,
    DRAWDOWN_NORMAL,
    Analysis,
    Base,
    CalibrationSnapshot,
    EquityCurve,
    ErrorLog,
    Event,
    Trade,
)
from src.positions.models import ERROR_RECONCILE_MISMATCH
from src.risk.budget import daily_llm_budget
from src.risk.drawdown import next_state, recovery_boundary
from src.risk.governor import RiskGovernor
from src.risk.kelly import (
    BUCKET_HIGH,
    BUCKET_LOW,
    BUCKET_MEDIUM,
    conviction_bucket,
    kelly_fraction,
    payoff_odds,
    position_fraction,
)
from src.risk.loop import GovernorLoop
from src.risk.models import P_SOURCE_CALIBRATED, P_SOURCE_FALLBACK
from src.sentinel.universe import UniverseEntry, UniverseIndex

RISK = RiskConfig()
CAPITAL = CapitalConfig()

UNIVERSE = UniverseIndex([
    UniverseEntry(ticker="AAA", sector="Biotech"),
    UniverseEntry(ticker="BBB", sector="Biotech"),
    UniverseEntry(ticker="CCC", sector="Tech"),
])


def test_kelly_fraction_matches_the_formula():
    assert kelly_fraction(0.5, 2.0) == pytest.approx(0.25)
    assert kelly_fraction(0.35, 1.0) == pytest.approx(-0.30)
    assert kelly_fraction(0.5, 0.0) == 0.0


def test_position_fraction_clamps_to_zero_and_cap():
    assert position_fraction(0.35, 1.0, 0.25, 0.10) == 0.0
    assert position_fraction(0.9, 5.0, 0.25, 0.10) == pytest.approx(0.10)


def test_conviction_buckets():
    assert conviction_bucket(0.60) == BUCKET_LOW
    assert conviction_bucket(0.65) == BUCKET_MEDIUM
    assert conviction_bucket(0.749) == BUCKET_MEDIUM
    assert conviction_bucket(0.75) == BUCKET_HIGH


def test_payoff_odds_long_option_uses_premium_leverage():
    odds = payoff_odds("LONG_CALL", 15.0, 150.0, 150.0, 30.0, [30.0])
    assert odds == pytest.approx(0.15 * (0.40 * 30.0 * 100.0 / 150.0))


def test_payoff_odds_spread_uses_width_over_debit():
    odds = payoff_odds("CALL_DEBIT_SPREAD", 15.0, 150.0, 150.0, 30.0, [30.0, 35.0])
    assert odds == pytest.approx((500.0 - 150.0) / 150.0)


def test_payoff_odds_stock_uses_move_over_stop():
    odds = payoff_odds("STOCK", 12.0, 30.0, 4.5, 30.0, [])
    assert odds == pytest.approx(0.12 / 0.15)


def test_payoff_odds_future_and_unknown_fall_back():
    assert payoff_odds("FUTURE", 20.0, 300.0, 300.0, None, []) == pytest.approx(2.0)
    assert payoff_odds("???", 20.0, 300.0, 300.0, None, []) == pytest.approx(1.0)


def test_daily_llm_budget_paper_keeps_the_configured_cap():
    assert daily_llm_budget(2450.0, "paper", 2.0) == 2.0


def test_daily_llm_budget_live_scales_with_equity():
    assert daily_llm_budget(2450.0, "live", 2.0) == pytest.approx(1.01)
    assert daily_llm_budget(24500.0, "live", 2.0) == pytest.approx(10.07)


def test_next_state_escalates_immediately():
    assert next_state(DRAWDOWN_NORMAL, 0.12, RISK) == DRAWDOWN_CAUTION
    assert next_state(DRAWDOWN_NORMAL, 0.25, RISK) == DRAWDOWN_DEFENSIVE
    assert next_state(DRAWDOWN_CAUTION, 0.35, RISK) == DRAWDOWN_HALT


def test_next_state_recovers_only_past_the_halfway_boundary():
    assert next_state(DRAWDOWN_CAUTION, 0.07, RISK) == DRAWDOWN_CAUTION
    assert next_state(DRAWDOWN_CAUTION, 0.04, RISK) == DRAWDOWN_NORMAL
    assert next_state(DRAWDOWN_DEFENSIVE, 0.17, RISK) == DRAWDOWN_DEFENSIVE
    assert next_state(DRAWDOWN_DEFENSIVE, 0.12, RISK) == DRAWDOWN_CAUTION
    assert next_state(DRAWDOWN_DEFENSIVE, 0.04, RISK) == DRAWDOWN_NORMAL


def test_halt_is_sticky_until_manual_reset():
    assert next_state(DRAWDOWN_HALT, 0.0, RISK) == DRAWDOWN_HALT


def test_recovery_boundaries():
    assert recovery_boundary(DRAWDOWN_CAUTION, RISK) == pytest.approx(0.05)
    assert recovery_boundary(DRAWDOWN_DEFENSIVE, RISK) == pytest.approx(0.15)
    assert recovery_boundary(DRAWDOWN_NORMAL, RISK) == 0.0


def structured_payload(
    choice: str = "CALL_DEBIT_SPREAD",
    net_debit: float = 150.0,
    underlying: float = 30.0,
    strikes: list[float] | None = None,
    outcome: str = "STRUCTURED",
) -> dict:
    strikes = strikes if strikes is not None else [30.0, 35.0]
    return {
        "outcome": outcome,
        "reason": "",
        "choice": choice,
        "contract": {
            "choice": choice,
            "legs": [
                {"spec": {"strike": strike}, "side": "BUY", "ratio": 1, "limit_price": 1.5}
                for strike in strikes
            ],
            "net_debit_per_unit": net_debit,
            "max_loss_per_unit": net_debit,
            "underlying_price": underlying,
            "iv": {},
            "notes": [],
        },
    }


def make_event(ticker: str, key: str) -> Event:
    return Event(
        source="EDGAR_8K",
        ticker=ticker,
        market="EQUITY",
        priority=2,
        dedup_key=key,
        status="ANALYZED",
        detected_at=datetime(2026, 7, 20, 12, 0),
    )


def make_analysis(
    event_id: int,
    ticker: str,
    payload: dict,
    conviction: float = 0.70,
    catalyst: str = "FDA_DECISION",
    move: float = 15.0,
) -> Analysis:
    return Analysis(
        event_id=event_id,
        ticker=ticker,
        decision="TRADE",
        conviction=Decimal(str(conviction)),
        expected_move_pct=Decimal(str(move)),
        catalyst_type=catalyst,
        structure_result=payload,
        structured_at=datetime(2026, 7, 20, 13, 0),
    )


def committed_trade(
    ticker: str,
    instrument: str = "OPTION",
    analysis_id: int | None = None,
    capital: float = 150.0,
) -> Trade:
    return Trade(
        analysis_id=analysis_id,
        ticker=ticker,
        market="EQUITY",
        instrument=instrument,
        direction="LONG",
        contract_spec={"multiplier": 100},
        quantity=1,
        capital_at_risk=Decimal(str(capital)),
        status="OPEN",
    )


def calibration_row(
    catalyst: str = "FDA_DECISION",
    bucket: str = BUCKET_MEDIUM,
    hit_rate: float = 0.50,
    n: int = 20,
) -> CalibrationSnapshot:
    return CalibrationSnapshot(
        catalyst_type=catalyst,
        conviction_bucket=bucket,
        sample_size=n,
        hit_rate=Decimal(str(hit_rate)),
        as_of=datetime(2026, 7, 28, 0, 0),
    )


def run_governor(
    analyses_spec,
    seed_trades=None,
    seed_committed=None,
    seed_equity=None,
    seed_calibration=None,
    risk: RiskConfig | None = None,
):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as session:
                stored_analyses = []
                for index, (ticker, payload, extra) in enumerate(analyses_spec):
                    event = make_event(ticker, f"evt-{index}")
                    session.add(event)
                    await session.flush()
                    analysis = make_analysis(event.id, ticker, payload, **extra)
                    session.add(analysis)
                    await session.flush()
                    stored_analyses.append(analysis)

                for index, (ticker, catalyst) in enumerate(seed_committed or []):
                    event = make_event(ticker, f"committed-{index}")
                    session.add(event)
                    await session.flush()
                    analysis = make_analysis(
                        event.id, ticker, structured_payload(), catalyst=catalyst
                    )
                    analysis.governed_at = datetime(2026, 7, 20, 14, 0)
                    session.add(analysis)
                    await session.flush()
                    session.add(committed_trade(ticker, analysis_id=analysis.id))

                for trade in (seed_trades or []):
                    session.add(trade)
                if seed_equity is not None:
                    session.add(seed_equity)
                for row in (seed_calibration or []):
                    session.add(row)
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

                governor = RiskGovernor(risk or RISK, CAPITAL, UNIVERSE)
                loop = GovernorLoop(governor)

                with patch(
                    "src.risk.loop.DatabaseManager.get_instance", return_value=FakeDB()
                ):
                    result = await loop.run()

                for analysis in stored_analyses:
                    await session.refresh(analysis)
                return result, stored_analyses
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def test_governor_approves_a_calibrated_spread():
    result, analyses = run_governor(
        [("AAA", structured_payload(), {})],
        seed_calibration=[calibration_row()],
    )
    verdict = analyses[0].risk_verdict

    assert result.approved == 1
    assert verdict["approved"] is True
    assert verdict["quantity"] == 1
    assert verdict["capital_at_risk"] == pytest.approx(150.0)
    assert verdict["hit_rate_source"] == P_SOURCE_CALIBRATED
    assert verdict["drawdown_state"] == DRAWDOWN_NORMAL
    assert analyses[0].governed_at is not None


def test_governor_halves_kelly_on_fallback_hit_rate():
    _, analyses = run_governor([("AAA", structured_payload(), {})])
    verdict = analyses[0].risk_verdict

    assert verdict["hit_rate_source"] == P_SOURCE_FALLBACK
    assert verdict["hit_rate_used"] == pytest.approx(RISK.fallback_hit_rate)


def test_governor_vetoes_negative_kelly():
    payload = structured_payload(choice="LONG_CALL", net_debit=400.0, strikes=[30.0])
    _, analyses = run_governor([("AAA", payload, {"move": 10.0})])
    verdict = analyses[0].risk_verdict

    assert verdict["approved"] is False
    assert "kelly sizing non-positive" in verdict["veto_reason"]


def test_governor_vetoes_below_minimum_size():
    payload = structured_payload(net_debit=400.0, strikes=[30.0, 40.0])
    _, analyses = run_governor(
        [("AAA", payload, {})],
        seed_calibration=[calibration_row()],
    )
    verdict = analyses[0].risk_verdict

    assert verdict["approved"] is False
    assert "below minimum size" in verdict["veto_reason"]


def test_governor_vetoes_duplicate_ticker():
    _, analyses = run_governor(
        [("AAA", structured_payload(), {})],
        seed_trades=[committed_trade("AAA")],
        seed_calibration=[calibration_row()],
    )
    assert "already has a committed position" in analyses[0].risk_verdict["veto_reason"]


def test_governor_vetoes_when_option_slots_are_full():
    risk = RiskConfig(max_concurrent_options=1)
    _, analyses = run_governor(
        [("AAA", structured_payload(), {})],
        seed_trades=[committed_trade("CCC")],
        seed_calibration=[calibration_row()],
        risk=risk,
    )
    assert "concurrent options limit" in analyses[0].risk_verdict["veto_reason"]


def test_governor_vetoes_futures_because_the_sleeve_is_deferred():
    risk = RiskConfig(max_concurrent_futures=0)
    payload = structured_payload(choice="FUTURE", net_debit=300.0, strikes=[])
    _, analyses = run_governor([("AAA", payload, {})], risk=risk)
    assert "futures limit reached" in analyses[0].risk_verdict["veto_reason"]


def test_governor_enforces_the_catalyst_limit():
    risk = RiskConfig(max_same_catalyst_type=1)
    _, analyses = run_governor(
        [("CCC", structured_payload(), {"catalyst": "FDA_DECISION"})],
        seed_committed=[("AAA", "FDA_DECISION")],
        seed_calibration=[calibration_row()],
        risk=risk,
    )
    assert "catalyst limit reached for FDA_DECISION" in analyses[0].risk_verdict["veto_reason"]


def test_governor_allows_a_different_catalyst_alongside():
    risk = RiskConfig(max_same_catalyst_type=1)
    _, analyses = run_governor(
        [("CCC", structured_payload(), {"catalyst": "MA"})],
        seed_committed=[("AAA", "FDA_DECISION")],
        seed_calibration=[calibration_row(catalyst="MA")],
        risk=risk,
    )
    assert analyses[0].risk_verdict["approved"] is True


def test_governor_enforces_the_sector_limit():
    risk = RiskConfig(max_same_sector=1)
    _, analyses = run_governor(
        [("AAA", structured_payload(), {})],
        seed_trades=[committed_trade("BBB")],
        seed_calibration=[calibration_row()],
        risk=risk,
    )
    assert "sector limit reached for Biotech" in analyses[0].risk_verdict["veto_reason"]


def test_governor_vetoes_everything_in_halt():
    halted = EquityCurve(
        equity=Decimal("1700.00"),
        high_water_mark=Decimal("2800.00"),
        drawdown_pct=Decimal("0.3929"),
        drawdown_state=DRAWDOWN_HALT,
        realized_pnl=Decimal("0"),
        open_premium=Decimal("0"),
        open_positions=0,
    )
    _, analyses = run_governor(
        [("AAA", structured_payload(), {})],
        seed_equity=halted,
        seed_calibration=[calibration_row()],
    )
    verdict = analyses[0].risk_verdict

    assert verdict["approved"] is False
    assert "HALT" in verdict["veto_reason"]


def test_governor_applies_the_caution_conviction_floor():
    caution = EquityCurve(
        equity=Decimal("2450.00"),
        high_water_mark=Decimal("2800.00"),
        drawdown_pct=Decimal("0.1250"),
        drawdown_state=DRAWDOWN_CAUTION,
        realized_pnl=Decimal("0"),
        open_premium=Decimal("0"),
        open_positions=0,
    )
    _, analyses = run_governor(
        [("AAA", structured_payload(), {"conviction": 0.60})],
        seed_equity=caution,
        seed_calibration=[calibration_row(bucket=BUCKET_LOW)],
    )
    verdict = analyses[0].risk_verdict

    assert verdict["approved"] is False
    assert "below the CAUTION floor" in verdict["veto_reason"]


def test_governor_vetoes_when_a_critical_error_is_unresolved():
    blocker = ErrorLog(
        component="ReconcileLoop",
        error_type=ERROR_RECONCILE_MISMATCH,
        message="positions diverge from the broker",
    )
    _, analyses = run_governor(
        [("AAA", structured_payload(), {})],
        seed_trades=[blocker],
        seed_calibration=[calibration_row()],
    )
    assert "blocked by unresolved critical error" in analyses[0].risk_verdict["veto_reason"]


def test_governor_skips_unstructured_analyses_without_a_verdict():
    payload = structured_payload(outcome="SKIP_HIGH_IV")
    result, analyses = run_governor([("AAA", payload, {})])

    assert result.unstructured == 1
    assert analyses[0].risk_verdict is None
    assert analyses[0].governed_at is not None


def test_governor_writes_an_equity_snapshot_when_it_runs():
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as session:
                event = make_event("AAA", "evt-snap")
                session.add(event)
                await session.flush()
                session.add(make_analysis(event.id, "AAA", structured_payload()))
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

                loop = GovernorLoop(RiskGovernor(RISK, CAPITAL, UNIVERSE))
                with patch(
                    "src.risk.loop.DatabaseManager.get_instance", return_value=FakeDB()
                ):
                    await loop.run()

                from src.database.repositories.equity import EquityRepository

                latest = await EquityRepository(session).get_latest()
                return latest
        finally:
            await engine.dispose()

    latest = asyncio.run(check())
    assert latest is not None
    assert float(latest.equity) == pytest.approx(CAPITAL.initial_usd)
    assert latest.drawdown_state == DRAWDOWN_NORMAL
