from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.models import InstrumentSpec, InstrumentType, OrderFill, OrderStatus
from src.core.clock import utcnow_naive
from src.core.config import PositionsConfig, RiskConfig
from src.core.market_clock import is_trading_day
from src.database.models import Base, ErrorLog, Event, Trade
from src.database.repositories import ErrorRepository
from src.executor.engine import ExecutionEngine
from src.positions.manager import PositionManager, trading_days_until
from src.positions.models import (
    ERROR_EXPIRY_CLOSE_FAILED,
    ERROR_RECONCILE_MISMATCH,
    EXIT_CONTRARY_EVENT,
    EXIT_HARD_EXPIRY,
    EXIT_HORIZON,
    EXIT_PREMIUM_STOP,
    EXIT_SCALE_OUT,
    EXIT_STOCK_STOP,
    EXIT_THETA,
    exit_decision,
)
from src.positions.reconcile import ReconcileLoop
from src.risk.drawdown import DrawdownTracker

CONFIG = PositionsConfig()


def decide(**overrides):
    defaults = dict(
        is_option=True,
        is_stock=False,
        value=1.50,
        entry=1.50,
        days_to_expiry=40,
        trading_days_to_expiry=28,
        horizon_elapsed=False,
        contrary_event=False,
        scaled_out=False,
        stop_fraction=None,
        config=CONFIG,
    )
    defaults.update(overrides)
    return exit_decision(**defaults)


def test_no_exit_when_nothing_triggers():
    assert decide() is None


def test_hard_expiry_beats_everything():
    decision = decide(trading_days_to_expiry=2, contrary_event=True)
    assert decision.reason == EXIT_HARD_EXPIRY
    assert decision.is_full


def test_contrary_event_closes_in_full():
    assert decide(contrary_event=True).reason == EXIT_CONTRARY_EVENT


def test_horizon_elapsed_closes_before_theta():
    decision = decide(horizon_elapsed=True, days_to_expiry=5)
    assert decision.reason == EXIT_HORIZON


def test_theta_exit_within_seven_days():
    assert decide(days_to_expiry=6).reason == EXIT_THETA


def test_premium_stop_at_minus_sixty_pct():
    assert decide(value=0.60).reason == EXIT_PREMIUM_STOP
    assert decide(value=0.61) is None


def test_stock_stop_uses_the_structured_stop_fraction():
    decision = decide(
        is_option=False, is_stock=True,
        value=24.0, entry=30.0,
        days_to_expiry=None, trading_days_to_expiry=None,
        stop_fraction=0.15,
    )
    assert decision.reason == EXIT_STOCK_STOP


def test_scale_out_at_plus_hundred_pct_is_partial():
    decision = decide(value=3.10)
    assert decision.reason == EXIT_SCALE_OUT
    assert decision.fraction == pytest.approx(0.5)
    assert not decision.is_full


def test_scale_out_fires_only_once():
    assert decide(value=3.10, scaled_out=True) is None


def test_trading_days_until_skips_weekends():
    assert trading_days_until(date(2026, 7, 24), date(2026, 7, 28)) == 2
    assert trading_days_until(date(2026, 7, 24), date(2026, 7, 24)) == 0


def next_trading_days_ahead(count: int) -> date:
    cursor = utcnow_naive().date()
    remaining = count
    while remaining > 0:
        cursor += timedelta(days=1)
        if is_trading_day(cursor):
            remaining -= 1
    return cursor


def pending_fill() -> OrderFill:
    return OrderFill("9", OrderStatus.SUBMITTED, 0, 0.0, 0.0)


def filled(quantity: int, price: float, commission: float = 1.3) -> OrderFill:
    return OrderFill("9", OrderStatus.FILLED, quantity, price, commission)


class FakeBroker:

    def __init__(self, bid: float, ask: float, fills=None, positions=None) -> None:
        self.bid = bid
        self.ask = ask
        self.fills = list(fills or [])
        self.positions = positions or []
        self.placed: list[tuple[str, str, int, float]] = []
        self.cancelled: list[str] = []
        self._next_order = 9

    def is_connected(self):
        return True

    def _quote(self):
        mid = (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else 0.0
        return SimpleNamespace(bid=self.bid, ask=self.ask, mid=mid)

    async def get_quote(self, spec):
        return self._quote()

    async def get_option_quote(self, spec):
        return self._quote()

    async def get_positions(self, force_refresh=False):
        return self.positions

    async def place_limit_order(self, spec, side, quantity, price, transmit=True):
        order_id = str(self._next_order)
        self._next_order += 1
        self.placed.append((spec.describe(), side.value, quantity, price))
        return SimpleNamespace(order_id=order_id)

    async def modify_order(self, order_id, price):
        return SimpleNamespace(success=True)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    async def get_order_fill(self, order_id):
        if len(self.fills) > 1:
            return self.fills.pop(0)
        if self.fills:
            return self.fills[0]
        return pending_fill()


class OpenClock:
    def can_trade(self):
        return True


class ClosedClock:
    def can_trade(self):
        return False


def option_leg(strike: float = 30.0, side: str = "BUY", expiry: date | None = None) -> dict:
    expiry_date = expiry or (utcnow_naive().date() + timedelta(days=40))
    spec = InstrumentSpec(
        instrument=InstrumentType.OPTION,
        symbol="AAA",
        expiry=expiry_date.strftime("%Y%m%d"),
        strike=strike,
        right="C",
        multiplier="100",
        con_id=int(100 + strike),
    )
    return {"spec": spec.to_dict(), "side": side, "ratio": 1}


def stock_leg() -> dict:
    spec = InstrumentSpec(
        instrument=InstrumentType.STOCK, symbol="AAA", multiplier="1", con_id=55
    )
    return {"spec": spec.to_dict(), "side": "BUY", "ratio": 1}


def open_trade(
    legs: list[dict] | None = None,
    instrument: str = "OPTION",
    entry: float = 1.50,
    quantity: int = 2,
    entry_days_ago: float = 1.0,
    horizon: int = 10,
    net_debit: float | None = None,
    max_loss: float | None = None,
    multiplier: float = 100.0,
) -> Trade:
    legs = legs if legs is not None else [option_leg()]
    return Trade(
        ticker="AAA",
        market="EQUITY",
        instrument=instrument,
        direction="LONG",
        contract_spec={
            "choice": "LONG_CALL",
            "legs": legs,
            "net_debit_per_unit": net_debit if net_debit is not None else entry * multiplier,
            "max_loss_per_unit": max_loss if max_loss is not None else entry * multiplier,
            "multiplier": multiplier,
        },
        con_id=101,
        quantity=quantity,
        entry_price=Decimal(str(entry)),
        entry_filled_at=utcnow_naive() - timedelta(days=entry_days_ago),
        commission_total=Decimal("1.3"),
        capital_at_risk=Decimal("300"),
        horizon_days=horizon,
        status="OPEN",
    )


def run_manager(trades, broker, events=None, clock=None, runs: int = 1):
    async def runner():
        engine_db = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine_db.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine_db, expire_on_commit=False)

        try:
            async with maker() as session:
                for item in (events or []) + list(trades):
                    session.add(item)
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

                manager = PositionManager(
                    broker,
                    ExecutionEngine(broker, wait_seconds=0, max_price_moves=4),
                    CONFIG,
                    clock or OpenClock(),
                    DrawdownTracker(RiskConfig(), 2450.0),
                )
                results = []
                with patch(
                    "src.positions.manager.DatabaseManager.get_instance",
                    return_value=FakeDB(),
                ):
                    for _ in range(runs):
                        results.append(await manager.run())

                for trade in trades:
                    await session.refresh(trade)
                errors = await ErrorRepository(session).unresolved(
                    [ERROR_EXPIRY_CLOSE_FAILED, ERROR_RECONCILE_MISMATCH]
                )
                return results, list(trades), errors
        finally:
            await engine_db.dispose()

    return asyncio.run(runner())


def test_manager_closes_on_premium_stop_with_commissions_in_pnl():
    trade = open_trade()
    broker = FakeBroker(0.55, 0.65, fills=[filled(2, 0.60)])

    results, trades, _ = run_manager([trade], broker)

    assert results[0].closed == 1
    closed = trades[0]
    assert closed.status == "CLOSED"
    assert closed.exit_reason == EXIT_PREMIUM_STOP
    assert float(closed.pnl_realized) == pytest.approx(-182.6)
    assert float(closed.commission_total) == pytest.approx(2.6)


def test_manager_scales_out_once_at_double():
    trade = open_trade()
    broker = FakeBroker(3.00, 3.20, fills=[filled(1, 3.10, 0.65)])

    results, trades, _ = run_manager([trade], broker, runs=2)

    assert results[0].scaled == 1
    assert results[1].scaled == 0
    survivor = trades[0]
    assert survivor.status == "OPEN"
    assert survivor.quantity == 1
    assert survivor.contract_spec.get("scaled_out") is True
    assert float(survivor.pnl_realized) == pytest.approx(159.35)
    assert len(broker.placed) == 1


def test_manager_closes_when_the_horizon_elapses():
    trade = open_trade(entry_days_ago=12.0, horizon=10)
    broker = FakeBroker(1.55, 1.65, fills=[filled(2, 1.60)])

    _, trades, _ = run_manager([trade], broker)

    assert trades[0].status == "CLOSED"
    assert trades[0].exit_reason == EXIT_HORIZON
    assert float(trades[0].pnl_realized) == pytest.approx(17.4)


def test_manager_closes_on_a_contrary_critical_event():
    trade = open_trade()
    contrary = Event(
        source="EDGAR_8K",
        ticker="AAA",
        market="EQUITY",
        direction="SHORT",
        priority=1,
        dedup_key="contrary-1",
        status="NEW",
        detected_at=utcnow_naive() - timedelta(hours=1),
    )
    broker = FakeBroker(1.45, 1.55, fills=[filled(2, 1.50)])

    _, trades, _ = run_manager([trade], broker, events=[contrary])

    assert trades[0].status == "CLOSED"
    assert trades[0].exit_reason == EXIT_CONTRARY_EVENT


def test_manager_blocks_the_system_when_expiry_close_fails():
    expiry = next_trading_days_ahead(1)
    trade = open_trade(legs=[option_leg(expiry=expiry)])
    broker = FakeBroker(1.45, 1.55, fills=[pending_fill()])

    results, trades, errors = run_manager([trade], broker)

    assert results[0].failed_exits == 1
    assert trades[0].status == "OPEN"
    assert errors and errors[0].error_type == ERROR_EXPIRY_CLOSE_FAILED


def test_manager_applies_the_stock_stop():
    trade = open_trade(
        legs=[stock_leg()],
        instrument="STOCK",
        entry=30.0,
        net_debit=30.0,
        max_loss=4.5,
        multiplier=1.0,
    )
    broker = FakeBroker(23.5, 24.5, fills=[filled(2, 24.0, 0.5)])

    _, trades, _ = run_manager([trade], broker)

    assert trades[0].status == "CLOSED"
    assert trades[0].exit_reason == EXIT_STOCK_STOP


def test_manager_stays_idle_outside_regular_hours():
    trade = open_trade()
    broker = FakeBroker(0.55, 0.65, fills=[filled(2, 0.60)])

    results, trades, _ = run_manager([trade], broker, clock=ClosedClock())

    assert results[0].market_closed
    assert trades[0].status == "OPEN"
    assert broker.placed == []


def test_manager_closes_a_spread_buying_the_short_leg_back_first():
    legs = [option_leg(30.0, "BUY"), option_leg(35.0, "SELL")]
    trade = open_trade(legs=legs, entry=1.25, net_debit=125.0, max_loss=125.0)
    broker = FakeBroker(
        1.00, 1.10,
        fills=[filled(2, 0.55, 0.65), filled(2, 1.80, 0.65)],
    )

    _, trades, _ = run_manager([trade], broker)

    closed = trades[0]
    assert closed.status == "CLOSED"
    assert broker.placed[0][1] == "BUY"
    assert "35.0" in broker.placed[0][0]
    assert broker.placed[1][1] == "SELL"
    assert float(closed.exit_price) == pytest.approx(1.25)
    assert float(closed.pnl_realized) == pytest.approx(-2.6)


def run_reconcile(trades, broker, runs: int = 1):
    async def runner():
        engine_db = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine_db.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine_db, expire_on_commit=False)

        try:
            async with maker() as session:
                for trade in trades:
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

                loop = ReconcileLoop(broker)
                results = []
                with patch(
                    "src.positions.reconcile.DatabaseManager.get_instance",
                    return_value=FakeDB(),
                ):
                    for _ in range(runs):
                        results.append(await loop.run())

                errors = await ErrorRepository(session).unresolved(
                    [ERROR_RECONCILE_MISMATCH]
                )
                return results, errors
        finally:
            await engine_db.dispose()

    return asyncio.run(runner())


def broker_position(con_id: int, quantity: int):
    return SimpleNamespace(spec=SimpleNamespace(con_id=con_id), quantity=quantity)


def test_reconcile_passes_when_positions_match():
    trade = open_trade()
    broker = FakeBroker(1.0, 1.1, positions=[broker_position(130, 2)])

    results, errors = run_reconcile([trade], broker)

    assert results[0].checked_con_ids == 1
    assert results[0].mismatches == 0
    assert errors == []


def test_reconcile_alerts_once_on_a_mismatch():
    trade = open_trade()
    broker = FakeBroker(1.0, 1.1, positions=[])

    results, errors = run_reconcile([trade], broker, runs=2)

    assert results[0].mismatches == 1
    assert results[0].alerted
    assert not results[1].alerted
    assert len(errors) == 1
    assert errors[0].error_type == ERROR_RECONCILE_MISMATCH
