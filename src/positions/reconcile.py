from __future__ import annotations

from collections import defaultdict

import structlog
from sqlalchemy import select

from src.broker.interface import BrokerInterface
from src.broker.models import OrderSide
from src.database.models import Trade
from src.database.repositories import ErrorRepository
from src.database.session import DatabaseManager
from src.positions.models import ERROR_RECONCILE_MISMATCH, ReconcileRunResult


class ReconcileLoop:

    def __init__(self, broker: BrokerInterface) -> None:
        self._broker = broker
        self._logger = structlog.get_logger("ReconcileLoop")

    async def run(self) -> ReconcileRunResult:
        result = ReconcileRunResult()

        if not self._broker.is_connected():
            self._logger.warning("reconcile_skipped", reason="broker disconnected")
            return result

        expected = defaultdict(int)
        db = DatabaseManager.get_instance()

        async with db.session() as session:
            rows = await session.execute(
                select(Trade).where(Trade.status == "OPEN")
            )
            for trade in rows.scalars().all():
                for leg in (trade.contract_spec or {}).get("legs") or []:
                    con_id = (leg.get("spec") or {}).get("con_id")
                    if con_id is None:
                        continue
                    sign = 1 if leg.get("side") == OrderSide.BUY.value else -1
                    expected[int(con_id)] += sign * trade.quantity

            if not expected:
                return result

            positions = await self._broker.get_positions(force_refresh=True)
            broker_quantities = {
                p.spec.con_id: p.quantity
                for p in positions
                if p.spec.con_id is not None
            }

            mismatches: list[dict] = []
            for con_id, quantity in expected.items():
                result.checked_con_ids += 1
                at_broker = broker_quantities.get(con_id, 0)
                if at_broker != quantity:
                    mismatches.append({
                        "con_id": con_id,
                        "expected": quantity,
                        "at_broker": at_broker,
                    })

            result.mismatches = len(mismatches)
            if mismatches:
                result.alerted = await ErrorRepository(session).record_once(
                    component="ReconcileLoop",
                    error_type=ERROR_RECONCILE_MISMATCH,
                    message=(
                        f"{len(mismatches)} position(s) diverge from the broker — "
                        f"new positions are blocked until resolved"
                    ),
                    context={"mismatches": mismatches},
                )
                self._logger.error(
                    "reconcile_mismatch",
                    mismatches=mismatches,
                    alert_created=result.alerted,
                )

        return result
