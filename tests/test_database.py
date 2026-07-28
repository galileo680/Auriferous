from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.contracts import option_spec
from src.broker.models import OptionRight
from src.database.models import (
    DRAWDOWN_CAUTION,
    DRAWDOWN_DEFENSIVE,
    DRAWDOWN_HALT,
    DRAWDOWN_NORMAL,
    EVENT_STATUS_NEW,
    EVENT_STATUS_TRIAGED,
    TRADE_STATUS_OPEN,
    TRADE_STATUS_PENDING,
    Base,
    Event,
    Trade,
)
from src.database.repositories import (
    DRAWDOWN_SIZE_MULTIPLIER,
    EventRepository,
    TradeRepository,
    classify_drawdown,
)


def run_with_db(body):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await body(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def make_event(ticker: str = "AMD", dedup_key: str = "edgar-0001") -> Event:
    return Event(
        source="EDGAR_8K",
        ticker=ticker,
        market="EQUITY",
        payload={"item": "2.02"},
        priority=2,
        dedup_key=dedup_key,
        status=EVENT_STATUS_NEW,
    )


def make_trade(
    ticker: str = "AMD",
    status: str = TRADE_STATUS_OPEN,
    con_id: int | None = 1001,
    capital: str = "200.00",
) -> Trade:
    spec = option_spec(ticker, "20260821", 150.0, OptionRight.CALL)
    return Trade(
        ticker=ticker,
        market="EQUITY",
        instrument="OPTION",
        direction="LONG",
        contract_spec=spec.to_dict(),
        con_id=con_id,
        quantity=1,
        entry_price=Decimal("2.00"),
        capital_at_risk=Decimal(capital),
        status=status,
        horizon_days=21,
    )


def test_event_dedup_detects_duplicate():
    async def body(session):
        repo = EventRepository(session)
        await repo.create(make_event())
        return (
            await repo.exists_dedup_key("edgar-0001"),
            await repo.exists_dedup_key("edgar-9999"),
        )

    duplicate, fresh = run_with_db(body)
    assert duplicate is True
    assert fresh is False


def test_pending_events_are_ordered_by_priority():
    async def body(session):
        repo = EventRepository(session)
        await repo.create(make_event("AAA", "k1"))
        low = make_event("BBB", "k2")
        low.priority = 5
        await repo.create(low)
        high = make_event("CCC", "k3")
        high.priority = 1
        await repo.create(high)
        return [e.ticker for e in await repo.get_pending()]

    assert run_with_db(body) == ["CCC", "AAA", "BBB"]


def test_marking_event_sets_processed_at():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event())
        await repo.mark(event.id, EVENT_STATUS_TRIAGED)
        return await repo.get_by_id(event.id)

    event = run_with_db(body)
    assert event.status == EVENT_STATUS_TRIAGED
    assert event.processed_at is not None


def test_pending_query_excludes_processed_events():
    async def body(session):
        repo = EventRepository(session)
        event = await repo.create(make_event())
        await repo.mark(event.id, EVENT_STATUS_TRIAGED)
        return await repo.get_pending()

    assert run_with_db(body) == []


def test_pending_trades_count_toward_commitments():
    async def body(session):
        repo = TradeRepository(session)
        await repo.create(make_trade("AMD", TRADE_STATUS_OPEN))
        await repo.create(make_trade("PLTR", TRADE_STATUS_PENDING, con_id=1002))
        return (
            await repo.count_committed(),
            await repo.ticker_is_committed("PLTR"),
            await repo.total_capital_at_risk(),
        )

    count, pending_owned, at_risk = run_with_db(body)
    assert count == 2
    assert pending_owned is True
    assert at_risk == Decimal("400.00")


def test_tracked_con_ids_returns_only_committed():
    async def body(session):
        repo = TradeRepository(session)
        await repo.create(make_trade("AMD", TRADE_STATUS_OPEN, con_id=1001))
        closed = make_trade("SOFI", TRADE_STATUS_OPEN, con_id=1003)
        await repo.create(closed)
        await repo.close(closed.id, exit_price=3.0, exit_reason="THESIS_INVALID")
        return await repo.get_tracked_con_ids()

    assert run_with_db(body) == {1001}


def test_close_computes_pnl_with_contract_multiplier():
    async def body(session):
        repo = TradeRepository(session)
        trade = await repo.create(make_trade())
        return await repo.close(trade.id, exit_price=3.50, exit_reason="SCALE_OUT")

    trade = run_with_db(body)
    assert trade.pnl_realized == Decimal("150.00")
    assert trade.exit_reason == "SCALE_OUT"


def test_close_is_idempotent():
    async def body(session):
        repo = TradeRepository(session)
        trade = await repo.create(make_trade())
        await repo.close(trade.id, exit_price=3.50, exit_reason="SCALE_OUT")
        return await repo.close(trade.id, exit_price=9.99, exit_reason="AGAIN")

    assert run_with_db(body) is None


def test_realized_pnl_total_sums_closed_trades():
    async def body(session):
        repo = TradeRepository(session)
        first = await repo.create(make_trade("AMD"))
        second = await repo.create(make_trade("PLTR", con_id=1002))
        await repo.close(first.id, exit_price=3.00, exit_reason="TP")
        await repo.close(second.id, exit_price=1.00, exit_reason="STOP")
        return await repo.realized_pnl_total()

    assert run_with_db(body) == Decimal("0.00")


def test_contract_spec_survives_the_database_roundtrip():
    async def body(session):
        repo = TradeRepository(session)
        trade = await repo.create(make_trade())
        return (await repo.get_by_id(trade.id)).contract_spec

    from src.broker.models import InstrumentSpec

    restored = InstrumentSpec.from_dict(run_with_db(body))
    assert restored.symbol == "AMD"
    assert restored.strike == 150.0
    assert restored.contract_multiplier == 100.0


@pytest.mark.parametrize(
    "drawdown,expected",
    [
        (0.00, DRAWDOWN_NORMAL),
        (0.09, DRAWDOWN_NORMAL),
        (0.10, DRAWDOWN_CAUTION),
        (0.19, DRAWDOWN_CAUTION),
        (0.20, DRAWDOWN_DEFENSIVE),
        (0.29, DRAWDOWN_DEFENSIVE),
        (0.30, DRAWDOWN_HALT),
        (0.55, DRAWDOWN_HALT),
    ],
)
def test_drawdown_classification_boundaries(drawdown, expected):
    assert classify_drawdown(drawdown, 0.10, 0.20, 0.30) == expected


def test_halt_state_blocks_all_sizing():
    assert DRAWDOWN_SIZE_MULTIPLIER[DRAWDOWN_HALT] == 0.0
    assert DRAWDOWN_SIZE_MULTIPLIER[DRAWDOWN_NORMAL] == 1.0
