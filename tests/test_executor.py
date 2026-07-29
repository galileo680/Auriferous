from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.models import (
    InstrumentSpec,
    InstrumentType,
    OrderFill,
    OrderSide,
    OrderStatus,
)
from src.core.clock import utcnow_naive
from src.database.models import (
    EVENT_STATUS_TRADED,
    TRADE_STATUS_EXPIRED,
    TRADE_STATUS_OPEN,
    TRADE_STATUS_PENDING,
    TRADE_STATUS_REJECTED,
    Analysis,
    Base,
    Event,
    Trade,
)
from src.executor.engine import ExecutionEngine, initial_limit, tick_size
from src.executor.loop import ExecutorLoop
from src.executor.models import ExecutionOutcome

OPTION_SPEC = InstrumentSpec(
    instrument=InstrumentType.OPTION,
    symbol="AAA",
    expiry="20260821",
    strike=30.0,
    right="C",
    multiplier="100",
    con_id=101,
)

STOCK_SPEC = InstrumentSpec(instrument=InstrumentType.STOCK, symbol="AAA")


def pending_fill(order_id: str = "7") -> OrderFill:
    return OrderFill(order_id, OrderStatus.SUBMITTED, 0, 0.0, 0.0)


def filled(quantity: int, price: float, commission: float = 1.3, order_id: str = "7") -> OrderFill:
    return OrderFill(order_id, OrderStatus.FILLED, quantity, price, commission)


def partial(quantity: int, price: float, order_id: str = "7") -> OrderFill:
    return OrderFill(order_id, OrderStatus.PARTIALLY_FILLED, quantity, price, 0.65)


class FakeBroker:

    def __init__(self, bid: float, ask: float, fills: list[OrderFill] | None = None) -> None:
        self.bid = bid
        self.ask = ask
        self.fills = list(fills or [])
        self.placed: list[tuple[str, str, int, float]] = []
        self.modifications: list[float] = []
        self.cancelled: list[str] = []
        self._next_order = 7

    async def get_quote(self, spec):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    async def get_option_quote(self, spec):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    async def place_limit_order(self, spec, side, quantity, price, transmit=True):
        order_id = str(self._next_order)
        self._next_order += 1
        self.placed.append((spec.describe(), side.value, quantity, price))
        return SimpleNamespace(order_id=order_id)

    async def modify_order(self, order_id, price):
        self.modifications.append(price)
        return SimpleNamespace(success=True)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    async def get_order_fill(self, order_id):
        if len(self.fills) > 1:
            return self.fills.pop(0)
        if self.fills:
            return self.fills[0]
        return pending_fill(order_id)

    async def check_margin(self, spec, side, quantity, limit_price):
        return SimpleNamespace(accepted=True, error=None)


def run_engine(broker: FakeBroker, side: OrderSide = OrderSide.BUY, quantity: int = 2):
    engine = ExecutionEngine(broker, wait_seconds=0, max_price_moves=4)
    return asyncio.run(engine.execute(OPTION_SPEC, side, quantity))


def test_tick_size_by_instrument():
    assert tick_size(OPTION_SPEC, 2.0) == 0.01
    assert tick_size(OPTION_SPEC, 5.0) == 0.05
    assert tick_size(STOCK_SPEC, 25.0) == 0.01
    assert tick_size(
        InstrumentSpec(instrument=InstrumentType.FUTURE, symbol="BFF"), 60000.0
    ) == 1.0


def test_initial_limit_starts_at_mid_rounded_to_tick():
    assert initial_limit(2.0, 0.01, OrderSide.BUY, 2.10) == pytest.approx(2.00)
    assert initial_limit(2.005, 0.01, OrderSide.BUY, 2.10) == pytest.approx(2.00)
    assert initial_limit(2.005, 0.01, OrderSide.SELL, 1.90) == pytest.approx(2.01)


def test_initial_limit_never_crosses_the_cap():
    assert initial_limit(2.30, 0.01, OrderSide.BUY, 2.10) == pytest.approx(2.10)
    assert initial_limit(1.70, 0.01, OrderSide.SELL, 1.90) == pytest.approx(1.90)


def test_engine_fills_at_mid_without_walking():
    broker = FakeBroker(1.90, 2.10, fills=[filled(2, 2.00)])
    result = run_engine(broker)

    assert result.outcome is ExecutionOutcome.FILLED
    assert result.filled_quantity == 2
    assert result.avg_price == pytest.approx(2.00)
    assert result.commission == pytest.approx(1.3)
    assert result.price_moves == 0
    assert broker.modifications == []
    assert broker.placed[0][3] == pytest.approx(2.00)


def test_engine_walks_the_limit_toward_the_ask():
    broker = FakeBroker(
        1.90, 2.10,
        fills=[pending_fill(), pending_fill(), filled(2, 2.02)],
    )
    result = run_engine(broker)

    assert result.outcome is ExecutionOutcome.FILLED
    assert result.price_moves == 2
    assert broker.modifications == [pytest.approx(2.01), pytest.approx(2.02)]


def test_engine_never_walks_past_the_ask():
    broker = FakeBroker(1.98, 2.02, fills=[pending_fill()])
    result = run_engine(broker)

    assert result.outcome is ExecutionOutcome.NO_FILL
    assert all(price <= 2.02 for price in broker.modifications)
    assert broker.modifications == [pytest.approx(2.01), pytest.approx(2.02)]
    assert broker.cancelled


def test_engine_cancels_after_four_moves_without_a_fill():
    broker = FakeBroker(1.90, 2.10, fills=[pending_fill()])
    result = run_engine(broker)

    assert result.outcome is ExecutionOutcome.NO_FILL
    assert result.price_moves == 4
    assert len(broker.modifications) == 4
    assert broker.cancelled
    assert "not filled after walking" in result.reason


def test_engine_accepts_a_partial_fill_after_cancel():
    fills = [pending_fill()] * 5 + [partial(1, 2.03)]
    broker = FakeBroker(1.90, 2.10, fills=fills)
    result = run_engine(broker)

    assert result.outcome is ExecutionOutcome.PARTIAL
    assert result.filled_quantity == 1
    assert broker.cancelled


def test_engine_refuses_a_one_sided_market():
    broker = FakeBroker(0.0, 2.10)
    result = run_engine(broker)

    assert result.outcome is ExecutionOutcome.NO_FILL
    assert "market order is never sent" in result.reason
    assert broker.placed == []


def test_engine_sell_walks_down_toward_the_bid():
    broker = FakeBroker(1.90, 2.10, fills=[pending_fill(), filled(2, 1.99)])
    result = run_engine(broker, side=OrderSide.SELL)

    assert result.outcome is ExecutionOutcome.FILLED
    assert broker.modifications == [pytest.approx(1.99)]
    assert all(price >= 1.90 for price in broker.modifications)


class OpenClock:
    def can_trade(self):
        return True


class ClosedClock:
    def can_trade(self):
        return False


def option_leg(strike: float = 30.0, side: str = "BUY") -> dict:
    spec = InstrumentSpec(
        instrument=InstrumentType.OPTION,
        symbol="AAA",
        expiry="20260821",
        strike=strike,
        right="C",
        multiplier="100",
        con_id=int(100 + strike),
    )
    return {"spec": spec.to_dict(), "side": side, "ratio": 1, "limit_price": 1.5}


def contract_payload(legs: list[dict] | None = None, choice: str = "LONG_CALL") -> dict:
    return {
        "outcome": "STRUCTURED",
        "reason": "",
        "choice": choice,
        "contract": {
            "choice": choice,
            "legs": legs if legs is not None else [option_leg()],
            "net_debit_per_unit": 150.0,
            "max_loss_per_unit": 150.0,
            "underlying_price": 30.0,
            "invalidation": ["thesis breaks"],
            "iv": {},
            "notes": [],
        },
    }


def approved_verdict(quantity: int = 2) -> dict:
    return {
        "approved": True,
        "quantity": quantity,
        "capital_at_risk": quantity * 150.0,
        "kelly_fraction_used": 0.05,
        "drawdown_state": "NORMAL",
        "veto_reason": None,
        "hit_rate_used": 0.5,
        "payoff_odds_used": 2.3,
        "hit_rate_source": "CALIBRATED",
    }


def seed_analysis(
    session_objects: list,
    ticker: str = "AAA",
    payload: dict | None = None,
    verdict: dict | None = None,
    governed_hours_ago: float = 1.0,
    key: str = "evt-exec",
) -> None:
    event = Event(
        source="EDGAR_8K",
        ticker=ticker,
        market="EQUITY",
        priority=2,
        dedup_key=key,
        status="ANALYZED",
        detected_at=utcnow_naive() - timedelta(hours=governed_hours_ago + 1),
    )
    session_objects.append(event)

    def build_analysis(event_id: int) -> Analysis:
        return Analysis(
            event_id=event_id,
            ticker=ticker,
            decision="TRADE",
            conviction=Decimal("0.70"),
            expected_move_pct=Decimal("15.0"),
            catalyst_type="FDA_DECISION",
            horizon_days=10,
            structure_result=payload if payload is not None else contract_payload(),
            structured_at=utcnow_naive() - timedelta(hours=governed_hours_ago),
            risk_verdict=verdict if verdict is not None else approved_verdict(),
            governed_at=utcnow_naive() - timedelta(hours=governed_hours_ago),
        )

    session_objects.append(build_analysis)


def run_executor(
    seeds,
    broker: FakeBroker,
    clock=None,
    extra_trades=None,
):
    async def runner():
        engine_db = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine_db.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine_db, expire_on_commit=False)

        try:
            async with maker() as session:
                last_event_id = None
                for item in seeds:
                    if callable(item):
                        session.add(item(last_event_id))
                    else:
                        session.add(item)
                        await session.flush()
                        if isinstance(item, Event):
                            last_event_id = item.id
                for trade in (extra_trades or []):
                    session.add(trade)
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

                loop = ExecutorLoop(
                    broker,
                    ExecutionEngine(broker, wait_seconds=0, max_price_moves=4),
                    clock or OpenClock(),
                )
                with patch(
                    "src.executor.loop.DatabaseManager.get_instance",
                    return_value=FakeDB(),
                ):
                    result = await loop.run()

                from sqlalchemy import select

                trades = list(
                    (await session.execute(select(Trade))).scalars().all()
                )
                events = list(
                    (await session.execute(select(Event))).scalars().all()
                )
                return result, trades, events
        finally:
            await engine_db.dispose()

    return asyncio.run(runner())


def test_executor_opens_a_position_and_marks_the_event():
    seeds: list = []
    seed_analysis(seeds)
    broker = FakeBroker(1.40, 1.60, fills=[filled(2, 1.50)])

    result, trades, events = run_executor(seeds, broker)

    assert result.opened == 1
    trade = trades[0]
    assert trade.status == TRADE_STATUS_OPEN
    assert trade.quantity == 2
    assert float(trade.entry_price) == pytest.approx(1.50)
    assert float(trade.commission_total) == pytest.approx(1.3)
    assert float(trade.capital_at_risk) == pytest.approx(300.0)
    assert trade.invalidation == ["thesis breaks"]
    assert events[0].status == EVENT_STATUS_TRADED


def test_executor_waits_when_the_market_is_closed():
    seeds: list = []
    seed_analysis(seeds)
    broker = FakeBroker(1.40, 1.60, fills=[filled(2, 1.50)])

    result, trades, _ = run_executor(seeds, broker, clock=ClosedClock())

    assert result.queued == 1
    assert trades == []
    assert broker.placed == []


def test_executor_expires_a_stale_approval_without_sending():
    seeds: list = []
    seed_analysis(seeds, governed_hours_ago=20.0)
    broker = FakeBroker(1.40, 1.60, fills=[filled(2, 1.50)])

    result, trades, events = run_executor(seeds, broker)

    assert result.stale == 1
    assert trades[0].status == TRADE_STATUS_EXPIRED
    assert "stale" in trades[0].exit_reason
    assert broker.placed == []
    assert events[0].status != EVENT_STATUS_TRADED


def test_executor_rejects_when_the_ticker_is_already_committed():
    seeds: list = []
    seed_analysis(seeds)
    committed = Trade(
        ticker="AAA",
        market="EQUITY",
        instrument="OPTION",
        direction="LONG",
        contract_spec={"multiplier": 100},
        quantity=1,
        capital_at_risk=Decimal("150"),
        status=TRADE_STATUS_OPEN,
    )
    broker = FakeBroker(1.40, 1.60, fills=[filled(2, 1.50)])

    result, trades, _ = run_executor(seeds, broker, extra_trades=[committed])

    assert result.rejected_precheck == 1
    rejected = [t for t in trades if t.status == TRADE_STATUS_REJECTED]
    assert rejected and "already committed" in rejected[0].exit_reason
    assert broker.placed == []


def test_executor_records_a_no_fill_without_marking_the_event():
    seeds: list = []
    seed_analysis(seeds)
    broker = FakeBroker(1.40, 1.60, fills=[pending_fill()])

    result, trades, events = run_executor(seeds, broker)

    assert result.no_fill == 1
    assert trades[0].status == TRADE_STATUS_REJECTED
    assert events[0].status != EVENT_STATUS_TRADED


def test_executor_ignores_vetoed_analyses():
    seeds: list = []
    seed_analysis(seeds, verdict={"approved": False, "veto_reason": "x"})
    broker = FakeBroker(1.40, 1.60, fills=[filled(2, 1.50)])

    result, trades, _ = run_executor(seeds, broker)

    assert result.examined == 0
    assert trades == []


def test_executor_cancels_stale_pending_orders_after_restart():
    stale = Trade(
        ticker="OLD",
        market="EQUITY",
        instrument="OPTION",
        direction="LONG",
        contract_spec={"multiplier": 100},
        quantity=1,
        capital_at_risk=Decimal("150"),
        status=TRADE_STATUS_PENDING,
        broker_order_id="42",
        opened_at=utcnow_naive() - timedelta(minutes=10),
    )
    broker = FakeBroker(1.40, 1.60)

    result, trades, _ = run_executor([], broker, extra_trades=[stale])

    assert result.expired_pending == 1
    assert trades[0].status == TRADE_STATUS_EXPIRED
    assert broker.cancelled == ["42"]


def test_executor_executes_a_spread_as_two_legs():
    seeds: list = []
    payload = contract_payload(
        legs=[option_leg(30.0, "BUY"), option_leg(35.0, "SELL")],
        choice="CALL_DEBIT_SPREAD",
    )
    seed_analysis(seeds, payload=payload)
    broker = FakeBroker(
        1.40, 1.90,
        fills=[filled(2, 1.80, 0.65, "7"), filled(2, 0.55, 0.65, "8")],
    )

    result, trades, _ = run_executor(seeds, broker)

    assert result.opened == 1
    trade = trades[0]
    assert trade.status == TRADE_STATUS_OPEN
    assert trade.instrument == "OPTION"
    assert float(trade.entry_price) == pytest.approx(1.25)
    assert float(trade.commission_total) == pytest.approx(1.30)
    assert len(broker.placed) == 2
    assert broker.placed[0][1] == "BUY"
    assert broker.placed[1][1] == "SELL"
