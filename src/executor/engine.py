from __future__ import annotations

import asyncio
import math

import structlog

from src.broker.interface import BrokerInterface
from src.broker.models import InstrumentSpec, InstrumentType, OrderSide, OrderStatus
from src.executor.models import ExecutionOutcome, ExecutionResult

WALK_WAIT_SECONDS = 15.0
MAX_PRICE_MOVES = 4

OPTION_PENNY_TICK_BELOW = 3.0


def tick_size(spec: InstrumentSpec, price: float) -> float:
    if spec.instrument is InstrumentType.OPTION:
        return 0.01 if price < OPTION_PENNY_TICK_BELOW else 0.05
    if spec.instrument is InstrumentType.FUTURE:
        return 1.0
    return 0.01


def initial_limit(mid: float, tick: float, side: OrderSide, cap: float) -> float:
    steps = round(mid / tick, 6)
    if side is OrderSide.BUY:
        price = math.floor(steps + 1e-9) * tick
        return round(min(price, cap), 4)
    price = math.ceil(steps - 1e-9) * tick
    return round(max(price, cap), 4)


class ExecutionEngine:

    def __init__(
        self,
        broker: BrokerInterface,
        wait_seconds: float = WALK_WAIT_SECONDS,
        max_price_moves: int = MAX_PRICE_MOVES,
    ) -> None:
        self._broker = broker
        self._wait_seconds = wait_seconds
        self._max_price_moves = max_price_moves
        self._logger = structlog.get_logger("ExecutionEngine")

    async def execute(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
    ) -> ExecutionResult:
        try:
            bid, ask = await self._market(spec)
        except Exception as e:
            return ExecutionResult(
                outcome=ExecutionOutcome.ERROR,
                requested_quantity=quantity,
                reason=f"quote unavailable: {e}",
            )

        if bid <= 0 or ask <= 0:
            return ExecutionResult(
                outcome=ExecutionOutcome.NO_FILL,
                requested_quantity=quantity,
                reason="no two-sided market — a market order is never sent instead",
            )

        mid = (bid + ask) / 2
        tick = tick_size(spec, mid)
        cap = ask if side is OrderSide.BUY else bid
        price = initial_limit(mid, tick, side, cap)

        try:
            placed = await self._broker.place_limit_order(spec, side, quantity, price)
        except Exception as e:
            return ExecutionResult(
                outcome=ExecutionOutcome.ERROR,
                requested_quantity=quantity,
                reason=f"order placement failed: {e}",
            )

        order_id = placed.order_id or ""
        moves = 0

        while True:
            await asyncio.sleep(self._wait_seconds)
            fill = await self._broker.get_order_fill(order_id)

            if fill.status is OrderStatus.FILLED:
                return ExecutionResult(
                    outcome=ExecutionOutcome.FILLED,
                    order_id=order_id,
                    requested_quantity=quantity,
                    filled_quantity=fill.filled_quantity or quantity,
                    avg_price=fill.avg_fill_price,
                    commission=fill.commission,
                    price_moves=moves,
                    final_limit=price,
                )

            if moves >= self._max_price_moves:
                break

            next_price = self._step(price, tick, side, cap)
            if next_price != price:
                price = next_price
                moves += 1
                await self._broker.modify_order(order_id, price)
                self._logger.info(
                    "limit_walked",
                    instrument=spec.describe(),
                    side=side.value,
                    price=price,
                    move=moves,
                )
            else:
                moves += 1

        await self._broker.cancel_order(order_id)
        fill = await self._broker.get_order_fill(order_id)

        if fill.filled_quantity >= quantity:
            outcome = ExecutionOutcome.FILLED
        elif fill.filled_quantity > 0:
            outcome = ExecutionOutcome.PARTIAL
        else:
            outcome = ExecutionOutcome.NO_FILL

        return ExecutionResult(
            outcome=outcome,
            order_id=order_id,
            requested_quantity=quantity,
            filled_quantity=fill.filled_quantity,
            avg_price=fill.avg_fill_price,
            commission=fill.commission,
            price_moves=moves,
            final_limit=price,
            reason="" if outcome is not ExecutionOutcome.NO_FILL else (
                "not filled after walking the limit to the market"
            ),
        )

    @staticmethod
    def _step(price: float, tick: float, side: OrderSide, cap: float) -> float:
        if side is OrderSide.BUY:
            return round(min(price + tick, cap), 4)
        return round(max(price - tick, cap), 4)

    async def _market(self, spec: InstrumentSpec) -> tuple[float, float]:
        if spec.is_option:
            quote = await self._broker.get_option_quote(spec)
        else:
            quote = await self._broker.get_quote(spec)
        return quote.bid, quote.ask
