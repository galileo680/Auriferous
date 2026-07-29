from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.clock import utcnow_naive
from src.database.models import (
    Analysis,
    Base,
    CalibrationSnapshot,
    Event,
    ShadowTrade,
    Trade,
)
from src.shadow.book import (
    BOOK_PARALLEL,
    BOOK_SHADOW,
    ORIGIN_GOVERNOR_VETO,
    ORIGIN_NO_FILL,
    ORIGIN_REDTEAM_VETO,
    ORIGIN_STRUCTURER_SKIP,
    ORIGIN_TRIAGE_REJECT,
    ShadowBookService,
)
from src.shadow.calibrator import Calibrator
from src.shadow.metrics import (
    manager_value,
    pearson,
    priced_in_calibration,
    triage_precision,
    veto_value,
)


def make_provider(prices: dict[str, float]):
    async def provider(tickers: list[str]) -> dict[str, float]:
        return {
            t.upper(): prices[t.upper()]
            for t in tickers
            if t.upper() in prices
        }

    return provider


class MarkBroker:

    def __init__(self, mid: float) -> None:
        self.mid = mid

    def is_connected(self) -> bool:
        return True

    async def get_option_quote(self, spec):
        return SimpleNamespace(mid=self.mid, bid=self.mid - 0.05, ask=self.mid + 0.05)

    async def get_quote(self, spec):
        return SimpleNamespace(mid=self.mid, bid=self.mid - 0.05, ask=self.mid + 0.05)


def make_event(ticker: str, key: str, direction: str | None = None, status: str = "ANALYZED") -> Event:
    return Event(
        source="EDGAR_8K",
        ticker=ticker,
        market="EQUITY",
        direction=direction,
        catalyst_type="FDA_DECISION",
        priority=2,
        dedup_key=key,
        status=status,
        detected_at=utcnow_naive() - timedelta(hours=2),
        triage_result={"reason": "not actionable"},
    )


def make_analysis(
    event_id: int,
    ticker: str,
    decision: str,
    direction: str | None = "LONG",
    conviction: float | None = 0.70,
    structure_result: dict | None = None,
    structured: bool = False,
    risk_verdict: dict | None = None,
    governed: bool = False,
    pricedin: dict | None = None,
) -> Analysis:
    return Analysis(
        event_id=event_id,
        ticker=ticker,
        decision=decision,
        direction=direction,
        conviction=Decimal(str(conviction)) if conviction is not None else None,
        expected_move_pct=Decimal("15.0"),
        catalyst_type="FDA_DECISION",
        horizon_days=10,
        veto_reason="killed" if decision != "TRADE" else None,
        structure_result=structure_result,
        structured_at=utcnow_naive() if structured else None,
        risk_verdict=risk_verdict,
        governed_at=utcnow_naive() if governed else None,
        pricedin_verdict=pricedin,
    )


def run_shadow(seed, provider, broker=None, runs: int = 1):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as session:
                await seed(session)
                await session.commit()

                service = ShadowBookService(prices=provider, broker=broker)
                results = []
                for _ in range(runs):
                    results.append(await service.sync(session))
                    await session.commit()

                rows = list(
                    (await session.execute(select(ShadowTrade))).scalars().all()
                )
                return results, rows, session, engine
        except Exception:
            await engine.dispose()
            raise

    return asyncio.run(_dispose_after(runner))


async def _dispose_after(runner):
    results, rows, session, engine = await runner()
    await engine.dispose()
    return results, rows


def test_redteam_veto_opens_a_short_aware_virtual_position():
    async def seed(session):
        event = make_event("AAA", "e1")
        session.add(event)
        await session.flush()
        session.add(make_analysis(event.id, "AAA", "VETO_REDTEAM", direction="SHORT"))

    results, rows = run_shadow(seed, make_provider({"AAA": 30.0}), runs=2)

    assert results[0].opened == 1
    assert results[1].opened == 0
    row = rows[0]
    assert row.origin == ORIGIN_REDTEAM_VETO
    assert row.book == BOOK_SHADOW
    assert row.quantity < 0
    assert float(row.entry_price) == pytest.approx(30.0)


def test_triage_reject_with_direction_is_shadowed_once():
    async def seed(session):
        session.add(make_event("BBB", "e2", direction="LONG", status="REJECTED"))

    results, rows = run_shadow(seed, make_provider({"BBB": 10.0}), runs=2)

    assert results[0].opened == 1
    assert results[1].opened == 0
    assert rows[0].origin == ORIGIN_TRIAGE_REJECT
    assert rows[0].event_id is not None
    assert rows[0].quantity == 100


def test_structurer_skip_and_governor_veto_get_their_origins():
    async def seed(session):
        first = make_event("AAA", "e3")
        second = make_event("CCC", "e4")
        session.add_all([first, second])
        await session.flush()
        session.add(make_analysis(
            first.id, "AAA", "TRADE",
            structure_result={"outcome": "SKIP_HIGH_IV", "reason": "iv too high"},
            structured=True,
        ))
        session.add(make_analysis(
            second.id, "CCC", "TRADE",
            structure_result={"outcome": "STRUCTURED"},
            structured=True,
            risk_verdict={"approved": False, "veto_reason": "sector limit"},
            governed=True,
        ))

    _, rows = run_shadow(seed, make_provider({"AAA": 30.0, "CCC": 20.0}))

    origins = {row.ticker: row.origin for row in rows}
    assert origins == {
        "AAA": ORIGIN_STRUCTURER_SKIP,
        "CCC": ORIGIN_GOVERNOR_VETO,
    }


def test_approved_and_executed_analyses_are_not_shadowed():
    async def seed(session):
        event = make_event("AAA", "e5")
        session.add(event)
        await session.flush()
        session.add(make_analysis(
            event.id, "AAA", "TRADE",
            structure_result={"outcome": "STRUCTURED"},
            structured=True,
            risk_verdict={"approved": True},
            governed=True,
        ))

    results, rows = run_shadow(seed, make_provider({"AAA": 30.0}))

    assert results[0].opened == 0
    assert rows == []


def test_no_fill_trade_lands_in_the_shadow_book():
    async def seed(session):
        session.add(Trade(
            ticker="DDD",
            market="EQUITY",
            instrument="OPTION",
            direction="LONG",
            contract_spec={"multiplier": 100},
            quantity=2,
            capital_at_risk=Decimal("0"),
            status="REJECTED_NO_FILL",
            exit_reason="not filled after walking the limit",
        ))

    _, rows = run_shadow(seed, make_provider({"DDD": 50.0}))

    assert rows[0].origin == ORIGIN_NO_FILL
    assert rows[0].trade_id is not None


def test_open_trade_gets_a_parallel_twin():
    async def seed(session):
        session.add(Trade(
            ticker="AAA",
            market="EQUITY",
            instrument="OPTION",
            direction="LONG",
            contract_spec={"multiplier": 100, "legs": []},
            quantity=2,
            entry_price=Decimal("1.50"),
            entry_filled_at=utcnow_naive(),
            capital_at_risk=Decimal("300"),
            horizon_days=10,
            status="OPEN",
        ))

    results, rows = run_shadow(seed, make_provider({}))

    assert results[0].opened_parallel == 1
    twin = rows[0]
    assert twin.book == BOOK_PARALLEL
    assert float(twin.entry_price) == pytest.approx(1.50)
    assert twin.quantity == 2


def test_time_exit_closes_a_short_with_a_signed_pnl():
    async def seed(session):
        session.add(ShadowTrade(
            ticker="AAA",
            book=BOOK_SHADOW,
            origin=ORIGIN_REDTEAM_VETO,
            catalyst_type="FDA_DECISION",
            conviction=Decimal("0.70"),
            entry_price=Decimal("30.0"),
            quantity=-33,
            expected_holding_days=10,
            status="OPEN",
            opened_at=utcnow_naive() - timedelta(days=12),
        ))

    results, rows = run_shadow(seed, make_provider({"AAA": 24.0}))

    assert results[0].closed == 1
    row = rows[0]
    assert row.status == "CLOSED"
    assert float(row.close_price) == pytest.approx(24.0)
    assert float(row.pnl_virtual) == pytest.approx((24.0 - 30.0) * -33)


def test_parallel_twin_is_marked_via_the_broker():
    async def seed(session):
        trade = Trade(
            ticker="AAA",
            market="EQUITY",
            instrument="OPTION",
            direction="LONG",
            contract_spec={
                "multiplier": 100,
                "legs": [{
                    "spec": {
                        "instrument": "OPTION", "symbol": "AAA",
                        "expiry": "20261218", "strike": 30.0, "right": "C",
                        "multiplier": "100",
                    },
                    "side": "BUY",
                }],
            },
            quantity=2,
            entry_price=Decimal("1.50"),
            capital_at_risk=Decimal("300"),
            status="CLOSED",
            pnl_realized=Decimal("50.0"),
        )
        session.add(trade)
        await session.flush()
        session.add(ShadowTrade(
            trade_id=trade.id,
            ticker="AAA",
            book=BOOK_PARALLEL,
            origin="PARALLEL",
            entry_price=Decimal("1.50"),
            quantity=2,
            expected_holding_days=10,
            status="OPEN",
            opened_at=utcnow_naive() - timedelta(days=12),
        ))

    results, rows = run_shadow(seed, make_provider({}), broker=MarkBroker(2.0))

    twins = [r for r in rows if r.book == BOOK_PARALLEL]
    assert results[0].closed == 1
    assert twins[0].status == "CLOSED"
    assert float(twins[0].pnl_virtual) == pytest.approx((2.0 - 1.5) * 2 * 100)


def closed_shadow(
    ticker: str,
    pnl: float,
    entry: float = 30.0,
    quantity: int = 33,
    conviction: float | None = 0.70,
    origin: str = ORIGIN_REDTEAM_VETO,
    analysis_id: int | None = None,
) -> ShadowTrade:
    return ShadowTrade(
        analysis_id=analysis_id,
        ticker=ticker,
        book=BOOK_SHADOW,
        origin=origin,
        catalyst_type="FDA_DECISION",
        conviction=Decimal(str(conviction)) if conviction is not None else None,
        entry_price=Decimal(str(entry)),
        quantity=quantity,
        expected_holding_days=10,
        status="CLOSED",
        opened_at=utcnow_naive() - timedelta(days=12),
        closed_at=utcnow_naive(),
        close_price=Decimal(str(entry + pnl / quantity)),
        pnl_virtual=Decimal(str(pnl)),
    )


def run_db(work):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def test_calibrator_buckets_and_writes_snapshots():
    async def work(session):
        session.add_all([
            closed_shadow("A1", 100.0),
            closed_shadow("A2", 50.0),
            closed_shadow("A3", -80.0),
            closed_shadow("A4", -70.0),
            closed_shadow("A5", 100.0, conviction=None),
        ])
        await session.flush()
        written = await Calibrator().run(session)
        snapshots = list(
            (await session.execute(select(CalibrationSnapshot))).scalars().all()
        )
        return written, snapshots

    written, snapshots = run_db(work)

    assert written == 1
    snapshot = snapshots[0]
    assert snapshot.catalyst_type == "FDA_DECISION"
    assert snapshot.sample_size == 4
    assert float(snapshot.hit_rate) == pytest.approx(0.5)


def test_calibrator_includes_closed_real_trades():
    async def work(session):
        event = make_event("AAA", "e9")
        session.add(event)
        await session.flush()
        analysis = make_analysis(event.id, "AAA", "TRADE", conviction=0.80)
        session.add(analysis)
        await session.flush()
        session.add(Trade(
            analysis_id=analysis.id,
            ticker="AAA",
            market="EQUITY",
            instrument="OPTION",
            direction="LONG",
            contract_spec={"multiplier": 100},
            quantity=2,
            capital_at_risk=Decimal("150"),
            status="CLOSED",
            pnl_realized=Decimal("75.0"),
        ))
        await session.flush()
        await Calibrator().run(session)
        return list(
            (await session.execute(select(CalibrationSnapshot))).scalars().all()
        )

    snapshots = run_db(work)

    assert len(snapshots) == 1
    assert snapshots[0].conviction_bucket == "HIGH"
    assert float(snapshots[0].hit_rate) == pytest.approx(1.0)


def test_veto_value_groups_by_origin():
    async def work(session):
        session.add_all([
            closed_shadow("A1", 100.0, origin=ORIGIN_REDTEAM_VETO),
            closed_shadow("A2", -40.0, origin=ORIGIN_REDTEAM_VETO),
            closed_shadow("A3", -90.0, origin=ORIGIN_TRIAGE_REJECT),
        ])
        await session.flush()
        return await veto_value(session)

    values = run_db(work)

    assert values[ORIGIN_REDTEAM_VETO] == pytest.approx(60.0)
    assert values[ORIGIN_TRIAGE_REJECT] == pytest.approx(-90.0)


def test_manager_value_compares_real_exits_with_the_parallel_book():
    async def work(session):
        trade = Trade(
            ticker="AAA",
            market="EQUITY",
            instrument="OPTION",
            direction="LONG",
            contract_spec={"multiplier": 100},
            quantity=2,
            capital_at_risk=Decimal("300"),
            status="CLOSED",
            pnl_realized=Decimal("120.0"),
        )
        session.add(trade)
        await session.flush()
        session.add(ShadowTrade(
            trade_id=trade.id,
            ticker="AAA",
            book=BOOK_PARALLEL,
            origin="PARALLEL",
            entry_price=Decimal("1.50"),
            quantity=2,
            expected_holding_days=10,
            status="CLOSED",
            pnl_virtual=Decimal("80.0"),
        ))
        await session.flush()
        return await manager_value(session)

    assert run_db(work) == pytest.approx(40.0)


def test_pearson_and_priced_in_calibration():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 2], [2, 4]) is None

    async def work(session):
        scores = [0.1, 0.5, 0.9]
        pnls = [198.0, 0.0, -198.0]
        for index, (score, pnl) in enumerate(zip(scores, pnls)):
            event = make_event(f"T{index}", f"pc-{index}")
            session.add(event)
            await session.flush()
            analysis = make_analysis(
                event.id, f"T{index}", "VETO_PRICEDIN",
                pricedin={"priced_in_score": score},
            )
            session.add(analysis)
            await session.flush()
            session.add(closed_shadow(f"T{index}", pnl, analysis_id=analysis.id))
        await session.flush()
        return await priced_in_calibration(session)

    correlation = run_db(work)
    assert correlation == pytest.approx(-1.0)


def test_triage_precision_counts_realized_catalysts():
    async def work(session):
        event = make_event("AAA", "tp-1")
        session.add(event)
        await session.flush()
        analysis = make_analysis(event.id, "AAA", "VETO_REDTEAM")
        session.add(analysis)
        await session.flush()
        session.add_all([
            closed_shadow("AAA", 198.0, analysis_id=analysis.id),
            closed_shadow("AAA", 30.0, analysis_id=analysis.id),
        ])
        await session.flush()
        return await triage_precision(session, min_move_pct=8.0)

    assert run_db(work) == pytest.approx(0.5)
