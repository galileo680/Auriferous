from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.interface import BrokerInterface
from src.broker.models import InstrumentSpec, OrderSide
from src.core.clock import utcnow_naive
from src.core.market_clock import MarketClock
from src.database.models import (
    EVENT_STATUS_TRADED,
    TRADE_STATUS_EXPIRED,
    TRADE_STATUS_OPEN,
    TRADE_STATUS_PENDING,
    TRADE_STATUS_REJECTED,
    Analysis,
    Trade,
)
from src.database.repositories import EventRepository, TradeRepository
from src.database.session import DatabaseManager
from src.executor.engine import ExecutionEngine
from src.executor.models import ExecutionOutcome, ExecutionResult
from src.risk.governor import CHOICE_TO_INSTRUMENT
from src.sentinel.models import MARKET_CRYPTO, MARKET_EQUITY

BATCH_SIZE = 5
APPROVAL_WINDOW_HOURS = 24
STALE_APPROVAL_HOURS = 18
STALE_PENDING_MINUTES = 5
SWARM_DECISION_TRADE = "TRADE"


@dataclass
class ExecutorRunResult:
    examined: int = 0
    opened: int = 0
    partial: int = 0
    no_fill: int = 0
    stale: int = 0
    rejected_precheck: int = 0
    expired_pending: int = 0
    queued: int = 0
    errors: int = 0


class ExecutorLoop:

    def __init__(
        self,
        broker: BrokerInterface,
        engine: ExecutionEngine,
        clock: MarketClock,
    ) -> None:
        self._broker = broker
        self._engine = engine
        self._clock = clock
        self._logger = structlog.get_logger("ExecutorLoop")

    async def run(self) -> ExecutorRunResult:
        result = ExecutorRunResult()
        db = DatabaseManager.get_instance()

        async with db.session() as session:
            await self._expire_stale_pending(session, result)

            approved = await self._pending_approved(session)
            if not approved:
                return result

            if not self._clock.can_trade():
                result.queued = len(approved)
                self._logger.info(
                    "executor_waiting_for_open",
                    queued=len(approved),
                    note="orders are sent only in the REGULAR session",
                )
                return result

            for analysis in approved[:BATCH_SIZE]:
                result.examined += 1
                try:
                    await self._execute_one(session, analysis, result)
                except Exception as e:
                    self._logger.error(
                        "executor_failed",
                        analysis_id=analysis.id,
                        ticker=analysis.ticker,
                        error=str(e),
                    )
                    result.errors += 1

        if result.examined or result.expired_pending or result.queued:
            self._logger.info(
                "executor_cycle",
                examined=result.examined,
                opened=result.opened,
                partial=result.partial,
                no_fill=result.no_fill,
                stale=result.stale,
                rejected_precheck=result.rejected_precheck,
                expired_pending=result.expired_pending,
                errors=result.errors,
            )
        return result

    async def _expire_stale_pending(
        self,
        session: AsyncSession,
        result: ExecutorRunResult,
    ) -> None:
        cutoff = utcnow_naive() - timedelta(minutes=STALE_PENDING_MINUTES)
        rows = await session.execute(
            select(Trade).where(
                Trade.status == TRADE_STATUS_PENDING,
                Trade.opened_at < cutoff,
            )
        )
        for trade in rows.scalars().all():
            if trade.broker_order_id:
                try:
                    await self._broker.cancel_order(trade.broker_order_id)
                except Exception as e:
                    self._logger.warning(
                        "stale_cancel_failed",
                        trade_id=trade.id,
                        order_id=trade.broker_order_id,
                        error=str(e),
                    )
            trade.status = TRADE_STATUS_EXPIRED
            trade.exit_reason = "stale pending order after restart"
            result.expired_pending += 1
            self._logger.warning("stale_pending_expired", trade_id=trade.id)

    async def _pending_approved(self, session: AsyncSession) -> list[Analysis]:
        cutoff = utcnow_naive() - timedelta(hours=APPROVAL_WINDOW_HOURS)
        has_trade = select(Trade.id).where(Trade.analysis_id == Analysis.id).exists()

        rows = await session.execute(
            select(Analysis)
            .where(
                Analysis.decision == SWARM_DECISION_TRADE,
                Analysis.governed_at.isnot(None),
                Analysis.governed_at >= cutoff,
                ~has_trade,
            )
            .order_by(Analysis.governed_at)
        )
        return [
            analysis for analysis in rows.scalars().all()
            if (analysis.risk_verdict or {}).get("approved") is True
        ]

    async def _execute_one(
        self,
        session: AsyncSession,
        analysis: Analysis,
        result: ExecutorRunResult,
    ) -> None:
        contract = (analysis.structure_result or {}).get("contract") or {}
        verdict = analysis.risk_verdict or {}
        quantity = int(verdict.get("quantity") or 0)
        choice = str(contract.get("choice") or "")
        kind = CHOICE_TO_INSTRUMENT.get(choice)

        if kind is None or quantity < 1 or not contract.get("legs"):
            await self._record_dead(
                session, analysis, contract, verdict,
                status=TRADE_STATUS_REJECTED,
                reason="approval payload is incomplete — nothing executable",
            )
            result.rejected_precheck += 1
            return

        if analysis.governed_at < utcnow_naive() - timedelta(hours=STALE_APPROVAL_HOURS):
            await self._record_dead(
                session, analysis, contract, verdict,
                status=TRADE_STATUS_EXPIRED,
                reason="approval stale — catalyst has likely been repriced",
            )
            result.stale += 1
            return

        trades = TradeRepository(session)
        if await trades.ticker_is_committed(analysis.ticker):
            await self._record_dead(
                session, analysis, contract, verdict,
                status=TRADE_STATUS_REJECTED,
                reason="ticker already committed at send time",
            )
            result.rejected_precheck += 1
            return

        legs = self._legs(contract)
        instrument = "OPTION" if kind == "OPTION_SPREAD" else kind
        max_loss = float(contract.get("max_loss_per_unit") or 0)

        if instrument == "FUTURE":
            spec, side = legs[0]
            reference = float(contract.get("underlying_price") or 0) or 1.0
            margin = await self._broker.check_margin(spec, side, quantity, reference)
            if not margin.accepted:
                await self._record_dead(
                    session, analysis, contract, verdict,
                    status=TRADE_STATUS_REJECTED,
                    reason=f"margin check failed: {margin.error or 'rejected'}",
                )
                result.rejected_precheck += 1
                return

        trade = Trade(
            analysis_id=analysis.id,
            ticker=analysis.ticker,
            market=MARKET_CRYPTO if instrument == "FUTURE" else MARKET_EQUITY,
            instrument=instrument,
            direction=analysis.direction or "LONG",
            contract_spec={**contract, "multiplier": legs[0][0].contract_multiplier},
            con_id=legs[0][0].con_id,
            quantity=quantity,
            capital_at_risk=Decimal(str(round(quantity * max_loss, 2))),
            kelly_fraction_used=Decimal(str(verdict.get("kelly_fraction_used") or 0)),
            invalidation=contract.get("invalidation"),
            horizon_days=analysis.horizon_days,
            status=TRADE_STATUS_PENDING,
            drawdown_state_at_entry=verdict.get("drawdown_state"),
        )
        session.add(trade)
        await session.flush()

        execution = await self._execute_legs(legs, quantity)

        trade.broker_order_id = execution.order_id

        if execution.outcome.opened_a_position:
            trade.status = TRADE_STATUS_OPEN
            trade.quantity = execution.filled_quantity
            trade.entry_price = Decimal(str(round(execution.avg_price, 4)))
            trade.entry_filled_at = utcnow_naive()
            trade.commission_total = Decimal(str(round(execution.commission, 2)))
            trade.capital_at_risk = Decimal(
                str(round(execution.filled_quantity * max_loss, 2))
            )
            await EventRepository(session).mark(analysis.event_id, EVENT_STATUS_TRADED)

            if execution.outcome is ExecutionOutcome.PARTIAL:
                result.partial += 1
            result.opened += 1
            self._logger.info(
                "position_opened",
                ticker=analysis.ticker,
                instrument=instrument,
                quantity=execution.filled_quantity,
                requested=quantity,
                entry_price=execution.avg_price,
                commission=execution.commission,
                price_moves=execution.price_moves,
            )
        else:
            trade.status = TRADE_STATUS_REJECTED
            trade.exit_reason = execution.reason or execution.outcome.value
            result.no_fill += 1
            self._logger.warning(
                "order_not_filled",
                ticker=analysis.ticker,
                outcome=execution.outcome.value,
                reason=execution.reason,
                price_moves=execution.price_moves,
            )

    async def _execute_legs(
        self,
        legs: list[tuple[InstrumentSpec, OrderSide]],
        quantity: int,
    ) -> ExecutionResult:
        if len(legs) == 1:
            spec, side = legs[0]
            return await self._engine.execute(spec, side, quantity)

        (long_spec, long_side), (short_spec, short_side) = legs[0], legs[1]

        long_result = await self._engine.execute(long_spec, long_side, quantity)
        if not long_result.outcome.opened_a_position:
            return long_result

        short_result = await self._engine.execute(
            short_spec, short_side, long_result.filled_quantity
        )

        if short_result.filled_quantity < long_result.filled_quantity:
            self._logger.warning(
                "spread_leg_imbalance",
                long_filled=long_result.filled_quantity,
                short_filled=short_result.filled_quantity,
                note="holding the long leg — risk stays defined, cost basis is higher",
            )

        hedge_ratio = (
            short_result.filled_quantity / long_result.filled_quantity
            if long_result.filled_quantity else 0.0
        )
        net_price = long_result.avg_price - short_result.avg_price * hedge_ratio

        return ExecutionResult(
            outcome=long_result.outcome,
            order_id=long_result.order_id,
            requested_quantity=quantity,
            filled_quantity=long_result.filled_quantity,
            avg_price=net_price,
            commission=long_result.commission + short_result.commission,
            price_moves=long_result.price_moves + short_result.price_moves,
            final_limit=long_result.final_limit,
        )

    async def _record_dead(
        self,
        session: AsyncSession,
        analysis: Analysis,
        contract: dict,
        verdict: dict,
        status: str,
        reason: str,
    ) -> None:
        legs = self._legs(contract) if contract.get("legs") else []
        multiplier = legs[0][0].contract_multiplier if legs else 1.0

        session.add(Trade(
            analysis_id=analysis.id,
            ticker=analysis.ticker,
            market=MARKET_EQUITY,
            instrument="OPTION",
            direction=analysis.direction or "LONG",
            contract_spec={**contract, "multiplier": multiplier},
            quantity=int(verdict.get("quantity") or 0),
            capital_at_risk=Decimal("0"),
            status=status,
            exit_reason=reason,
            drawdown_state_at_entry=verdict.get("drawdown_state"),
        ))
        await session.flush()
        self._logger.info(
            "execution_skipped",
            ticker=analysis.ticker,
            status=status,
            reason=reason,
        )

    @staticmethod
    def _legs(contract: dict) -> list[tuple[InstrumentSpec, OrderSide]]:
        return [
            (InstrumentSpec.from_dict(leg["spec"]), OrderSide(leg["side"]))
            for leg in (contract.get("legs") or [])
        ]
