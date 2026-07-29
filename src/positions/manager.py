from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.interface import BrokerInterface
from src.broker.models import InstrumentSpec, OrderSide
from src.core.clock import utcnow_naive
from src.core.config import PositionsConfig
from src.core.market_clock import MarketClock, is_trading_day
from src.database.models import Event, Trade
from src.database.repositories import ErrorRepository, TradeRepository
from src.database.session import DatabaseManager
from src.executor.engine import ExecutionEngine
from src.executor.models import ExecutionOutcome, ExecutionResult
from src.positions.models import (
    ERROR_EXPIRY_CLOSE_FAILED,
    EXIT_HARD_EXPIRY,
    EXIT_SCALE_OUT,
    ExitDecision,
    ManagerRunResult,
    exit_decision,
)
from src.risk.drawdown import DrawdownTracker

CONTRARY_PRIORITY = 1


def trading_days_until(today: date, expiry: date) -> int:
    if expiry <= today:
        return 0
    count = 0
    cursor = today
    while cursor < expiry:
        cursor += timedelta(days=1)
        if is_trading_day(cursor):
            count += 1
    return count


class PositionManager:

    def __init__(
        self,
        broker: BrokerInterface,
        engine: ExecutionEngine,
        config: PositionsConfig,
        clock: MarketClock,
        tracker: DrawdownTracker,
    ) -> None:
        self._broker = broker
        self._engine = engine
        self._config = config
        self._clock = clock
        self._tracker = tracker
        self._logger = structlog.get_logger("PositionManager")

    async def run(self) -> ManagerRunResult:
        result = ManagerRunResult()

        if not self._clock.can_trade():
            result.market_closed = True
            return result

        db = DatabaseManager.get_instance()
        async with db.session() as session:
            trades = await self._open_trades(session)
            if not trades:
                await self._tracker.snapshot(session, unrealized_pnl=0.0)
                return result

            today = utcnow_naive().date()

            for trade in trades:
                result.examined += 1
                try:
                    await self._manage_one(session, trade, today, result)
                except Exception as e:
                    self._logger.error(
                        "position_manage_failed",
                        trade_id=trade.id,
                        ticker=trade.ticker,
                        error=str(e),
                    )
                    result.errors += 1

            await self._tracker.snapshot(session, unrealized_pnl=result.unrealized_pnl)

        if result.examined:
            self._logger.info(
                "position_cycle",
                examined=result.examined,
                closed=result.closed,
                scaled=result.scaled,
                failed_exits=result.failed_exits,
                unrealized_pnl=round(result.unrealized_pnl, 2),
                by_reason=result.by_reason,
            )
        return result

    async def _open_trades(self, session: AsyncSession) -> list[Trade]:
        rows = await session.execute(
            select(Trade).where(Trade.status == "OPEN").order_by(Trade.opened_at)
        )
        return list(rows.scalars().all())

    async def _manage_one(
        self,
        session: AsyncSession,
        trade: Trade,
        today: date,
        result: ManagerRunResult,
    ) -> None:
        if trade.instrument == "FUTURE":
            self._logger.warning(
                "future_position_unmanaged",
                trade_id=trade.id,
                note="BFF sleeve is deferred — manage this position manually",
            )
            return

        contract = trade.contract_spec or {}
        legs = self._legs(contract)
        if not legs:
            self._logger.error("position_without_legs", trade_id=trade.id)
            return

        value = await self._mark(legs)
        entry = float(trade.entry_price or 0)
        multiplier = float(contract.get("multiplier") or 1)

        if value is not None and entry > 0:
            result.unrealized_pnl += (value - entry) * trade.quantity * multiplier

        expiry = self._earliest_expiry(legs)
        is_option = trade.instrument == "OPTION"

        net_debit = float(contract.get("net_debit_per_unit") or 0)
        max_loss = float(contract.get("max_loss_per_unit") or 0)
        stop_fraction = max_loss / net_debit if net_debit > 0 else None

        decision = exit_decision(
            is_option=is_option,
            is_stock=trade.instrument == "STOCK",
            value=value,
            entry=entry,
            days_to_expiry=(expiry - today).days if expiry else None,
            trading_days_to_expiry=trading_days_until(today, expiry) if expiry else None,
            horizon_elapsed=self._horizon_elapsed(trade),
            contrary_event=await self._has_contrary_event(session, trade),
            scaled_out=bool(contract.get("scaled_out")),
            stop_fraction=stop_fraction,
            config=self._config,
        )
        if decision is None:
            return

        await self._exit(session, trade, legs, decision, value, multiplier, result)

    async def _exit(
        self,
        session: AsyncSession,
        trade: Trade,
        legs: list[tuple[InstrumentSpec, OrderSide]],
        decision: ExitDecision,
        value: Optional[float],
        multiplier: float,
        result: ManagerRunResult,
    ) -> None:
        quantity = (
            trade.quantity if decision.is_full
            else max(int(trade.quantity * decision.fraction), 1)
        )
        if not decision.is_full and quantity >= trade.quantity:
            return

        execution = await self._close_legs(legs, quantity)

        if execution is None or execution.filled_quantity < 1:
            result.failed_exits += 1
            self._logger.warning(
                "exit_not_filled",
                trade_id=trade.id,
                ticker=trade.ticker,
                reason=decision.reason,
            )
            if decision.reason == EXIT_HARD_EXPIRY:
                created = await ErrorRepository(session).record_once(
                    component="PositionManager",
                    error_type=ERROR_EXPIRY_CLOSE_FAILED,
                    message=(
                        f"could not close {trade.ticker} (trade {trade.id}) before "
                        f"expiry — ITM auto-exercise risk; new positions are blocked"
                    ),
                    context={"trade_id": trade.id, "ticker": trade.ticker},
                )
                if created:
                    self._logger.error(
                        "expiry_close_failed",
                        trade_id=trade.id,
                        ticker=trade.ticker,
                    )
            return

        closed_quantity = min(execution.filled_quantity, quantity)
        exit_price = execution.avg_price
        gross = (exit_price - float(trade.entry_price or 0)) * closed_quantity * multiplier
        prior_pnl = float(trade.pnl_realized or 0)

        if decision.reason == EXIT_SCALE_OUT or closed_quantity < trade.quantity:
            trade.quantity -= closed_quantity
            trade.pnl_realized = Decimal(
                str(round(prior_pnl + gross - execution.commission, 2))
            )
            trade.commission_total = Decimal(
                str(round(float(trade.commission_total or 0) + execution.commission, 2))
            )
            if decision.reason == EXIT_SCALE_OUT:
                trade.contract_spec = {**trade.contract_spec, "scaled_out": True}
            result.scaled += 1
            result.by_reason[decision.reason] = result.by_reason.get(decision.reason, 0) + 1
            self._logger.info(
                "position_scaled_out",
                trade_id=trade.id,
                ticker=trade.ticker,
                sold=closed_quantity,
                remaining=trade.quantity,
                exit_price=exit_price,
            )
            return

        entry_commission = float(trade.commission_total or 0)
        pnl = prior_pnl + gross - execution.commission - entry_commission

        trade.commission_total = Decimal(
            str(round(entry_commission + execution.commission, 2))
        )
        await TradeRepository(session).close(
            trade.id,
            exit_price=exit_price,
            exit_reason=decision.reason,
            pnl=Decimal(str(round(pnl, 2))),
        )
        result.closed += 1
        result.by_reason[decision.reason] = result.by_reason.get(decision.reason, 0) + 1
        self._logger.info(
            "position_closed",
            trade_id=trade.id,
            ticker=trade.ticker,
            reason=decision.reason,
            exit_price=exit_price,
            pnl=round(pnl, 2),
        )

    async def _close_legs(
        self,
        legs: list[tuple[InstrumentSpec, OrderSide]],
        quantity: int,
    ) -> Optional[ExecutionResult]:
        closing = [
            (spec, OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY)
            for spec, side in legs
        ]
        closing.sort(key=lambda leg: 0 if leg[1] is OrderSide.BUY else 1)

        total_commission = 0.0
        net_price = 0.0
        filled = None
        order_id = None

        for spec, side in closing:
            execution = await self._engine.execute(spec, side, quantity)
            if not execution.outcome.opened_a_position:
                if filled is None:
                    return execution
                self._logger.warning(
                    "exit_leg_unfilled",
                    instrument=spec.describe(),
                    note="one leg closed, the other remains — retrying next cycle",
                )
                continue

            total_commission += execution.commission
            signed = execution.avg_price if side is OrderSide.SELL else -execution.avg_price
            net_price += signed
            filled = (
                execution.filled_quantity if filled is None
                else min(filled, execution.filled_quantity)
            )
            order_id = order_id or execution.order_id

        if filled is None:
            return None

        return ExecutionResult(
            outcome=ExecutionOutcome.FILLED,
            order_id=order_id,
            requested_quantity=quantity,
            filled_quantity=filled,
            avg_price=net_price,
            commission=total_commission,
        )

    async def _mark(
        self,
        legs: list[tuple[InstrumentSpec, OrderSide]],
    ) -> Optional[float]:
        net = 0.0
        for spec, side in legs:
            try:
                if spec.is_option:
                    quote = await self._broker.get_option_quote(spec)
                else:
                    quote = await self._broker.get_quote(spec)
            except Exception as e:
                self._logger.warning(
                    "mark_unavailable", instrument=spec.describe(), error=str(e)
                )
                return None

            mid = quote.mid
            if mid <= 0:
                return None
            net += mid if side is OrderSide.BUY else -mid
        return net

    def _horizon_elapsed(self, trade: Trade) -> bool:
        if not trade.entry_filled_at or not trade.horizon_days:
            return False
        deadline = trade.entry_filled_at + timedelta(days=int(trade.horizon_days))
        return utcnow_naive() >= deadline

    async def _has_contrary_event(self, session: AsyncSession, trade: Trade) -> bool:
        if not trade.entry_filled_at:
            return False
        opposite = "SHORT" if (trade.direction or "LONG") == "LONG" else "LONG"
        row = await session.execute(
            select(Event.id).where(
                Event.ticker == trade.ticker,
                Event.detected_at > trade.entry_filled_at,
                Event.priority == CONTRARY_PRIORITY,
                Event.direction == opposite,
            ).limit(1)
        )
        return row.scalar_one_or_none() is not None

    @staticmethod
    def _legs(contract: dict) -> list[tuple[InstrumentSpec, OrderSide]]:
        return [
            (InstrumentSpec.from_dict(leg["spec"]), OrderSide(leg["side"]))
            for leg in (contract.get("legs") or [])
        ]

    @staticmethod
    def _earliest_expiry(
        legs: list[tuple[InstrumentSpec, OrderSide]],
    ) -> Optional[date]:
        expiries = [
            datetime.strptime(spec.expiry, "%Y%m%d").date()
            for spec, _ in legs
            if spec.expiry
        ]
        return min(expiries) if expiries else None
