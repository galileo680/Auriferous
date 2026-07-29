from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Awaitable, Callable, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.interface import BrokerInterface
from src.broker.models import InstrumentSpec, OrderSide
from src.core.clock import utcnow_naive
from src.database.models import (
    EVENT_STATUS_REJECTED,
    TRADE_STATUS_OPEN,
    TRADE_STATUS_REJECTED,
    Analysis,
    Event,
    ShadowTrade,
    Trade,
)

BOOK_SHADOW = "SHADOW"
BOOK_PARALLEL = "PARALLEL"

ORIGIN_TRIAGE_REJECT = "TRIAGE_REJECT"
ORIGIN_REDTEAM_VETO = "REDTEAM_VETO"
ORIGIN_PRICEDIN_VETO = "PRICEDIN_VETO"
ORIGIN_LOW_CONVICTION = "LOW_CONVICTION"
ORIGIN_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
ORIGIN_STRUCTURER_SKIP = "STRUCTURER_SKIP"
ORIGIN_GOVERNOR_VETO = "GOVERNOR_VETO"
ORIGIN_NO_FILL = "NO_FILL"
ORIGIN_PARALLEL = "PARALLEL"

DECISION_TO_ORIGIN = {
    "VETO_REDTEAM": ORIGIN_REDTEAM_VETO,
    "VETO_PRICEDIN": ORIGIN_PRICEDIN_VETO,
    "VETO_LOW_CONVICTION": ORIGIN_LOW_CONVICTION,
    "BUDGET_EXHAUSTED": ORIGIN_BUDGET_EXHAUSTED,
}

VIRTUAL_NOTIONAL_USD = 1000.0
DEFAULT_HOLDING_DAYS = 10
BACKFILL_WINDOW_HOURS = 48

PriceProvider = Callable[[list[str]], Awaitable[dict[str, float]]]


@dataclass
class ShadowSyncResult:
    opened: int = 0
    opened_parallel: int = 0
    closed: int = 0
    skipped_no_price: int = 0
    by_origin: dict[str, int] = field(default_factory=dict)


class ShadowBookService:

    def __init__(
        self,
        prices: PriceProvider,
        broker: Optional[BrokerInterface] = None,
        parallel_enabled: bool = True,
    ) -> None:
        self._prices = prices
        self._broker = broker
        self._parallel_enabled = parallel_enabled
        self._logger = structlog.get_logger("ShadowBook")

    async def sync(self, session: AsyncSession) -> ShadowSyncResult:
        result = ShadowSyncResult()
        candidates = await self._collect_candidates(session)

        tickers = [c["ticker"] for c in candidates]
        prices = await self._prices(tickers) if tickers else {}

        for candidate in candidates:
            price = prices.get(candidate["ticker"].upper())
            if price is None or price <= 0:
                result.skipped_no_price += 1
                continue

            quantity = max(int(VIRTUAL_NOTIONAL_USD / price), 1)
            if candidate["direction"] == "SHORT":
                quantity = -quantity

            session.add(ShadowTrade(
                event_id=candidate.get("event_id"),
                analysis_id=candidate.get("analysis_id"),
                trade_id=candidate.get("trade_id"),
                ticker=candidate["ticker"],
                book=BOOK_SHADOW,
                origin=candidate["origin"],
                origin_detail=candidate.get("detail"),
                catalyst_type=candidate.get("catalyst_type"),
                conviction=candidate.get("conviction"),
                entry_price=Decimal(str(round(price, 4))),
                quantity=quantity,
                expected_holding_days=candidate.get("holding_days") or DEFAULT_HOLDING_DAYS,
                status="OPEN",
            ))
            result.opened += 1
            origin = candidate["origin"]
            result.by_origin[origin] = result.by_origin.get(origin, 0) + 1

        await session.flush()

        if self._parallel_enabled:
            result.opened_parallel = await self._open_parallel(session)

        result.closed = await self._close_due(session)

        if result.opened or result.opened_parallel or result.closed:
            self._logger.info(
                "shadow_sync",
                opened=result.opened,
                opened_parallel=result.opened_parallel,
                closed=result.closed,
                skipped_no_price=result.skipped_no_price,
                by_origin=result.by_origin,
            )
        return result

    async def _collect_candidates(self, session: AsyncSession) -> list[dict]:
        cutoff = utcnow_naive() - timedelta(hours=BACKFILL_WINDOW_HOURS)
        candidates: list[dict] = []

        shadowed_event = (
            select(ShadowTrade.id)
            .where(ShadowTrade.event_id == Event.id)
            .exists()
        )
        events = await session.execute(
            select(Event).where(
                Event.status == EVENT_STATUS_REJECTED,
                Event.direction.in_(["LONG", "SHORT"]),
                Event.detected_at >= cutoff,
                ~shadowed_event,
            )
        )
        for event in events.scalars().all():
            candidates.append({
                "event_id": event.id,
                "ticker": event.ticker,
                "direction": event.direction,
                "origin": ORIGIN_TRIAGE_REJECT,
                "catalyst_type": event.catalyst_type,
                "detail": (event.triage_result or {}).get("reason"),
            })

        shadowed_analysis = (
            select(ShadowTrade.id)
            .where(ShadowTrade.analysis_id == Analysis.id)
            .exists()
        )
        analyses = await session.execute(
            select(Analysis, Event.direction)
            .join(Event, Event.id == Analysis.event_id)
            .where(Analysis.created_at >= cutoff, ~shadowed_analysis)
        )
        for analysis, event_direction in analyses.all():
            candidate = self._analysis_candidate(analysis, event_direction)
            if candidate is not None:
                candidates.append(candidate)

        shadowed_trade = (
            select(ShadowTrade.id)
            .where(ShadowTrade.trade_id == Trade.id)
            .exists()
        )
        rejected = await session.execute(
            select(Trade).where(
                Trade.status == TRADE_STATUS_REJECTED,
                Trade.opened_at >= cutoff,
                ~shadowed_trade,
            )
        )
        for trade in rejected.scalars().all():
            candidates.append({
                "trade_id": trade.id,
                "analysis_id": trade.analysis_id,
                "ticker": trade.ticker,
                "direction": trade.direction or "LONG",
                "origin": ORIGIN_NO_FILL,
                "detail": trade.exit_reason,
                "holding_days": trade.horizon_days,
            })

        return candidates

    def _analysis_candidate(
        self,
        analysis: Analysis,
        event_direction: Optional[str],
    ) -> Optional[dict]:
        direction = analysis.direction or event_direction
        if direction not in ("LONG", "SHORT"):
            return None

        base = {
            "analysis_id": analysis.id,
            "ticker": analysis.ticker,
            "direction": direction,
            "catalyst_type": analysis.catalyst_type,
            "conviction": analysis.conviction,
            "holding_days": analysis.horizon_days,
        }

        origin = DECISION_TO_ORIGIN.get(analysis.decision)
        if origin is not None:
            return {**base, "origin": origin, "detail": analysis.veto_reason}

        if analysis.decision != "TRADE":
            return None

        structure = analysis.structure_result or {}
        if analysis.structured_at is not None and structure.get("outcome") != "STRUCTURED":
            return {
                **base,
                "origin": ORIGIN_STRUCTURER_SKIP,
                "detail": structure.get("reason"),
            }

        verdict = analysis.risk_verdict or {}
        if analysis.governed_at is not None and verdict.get("approved") is False:
            return {
                **base,
                "origin": ORIGIN_GOVERNOR_VETO,
                "detail": verdict.get("veto_reason"),
            }

        return None

    async def _open_parallel(self, session: AsyncSession) -> int:
        twinned = (
            select(ShadowTrade.id)
            .where(
                ShadowTrade.trade_id == Trade.id,
                ShadowTrade.book == BOOK_PARALLEL,
            )
            .exists()
        )
        rows = await session.execute(
            select(Trade).where(
                Trade.status == TRADE_STATUS_OPEN,
                Trade.entry_price.isnot(None),
                ~twinned,
            )
        )

        opened = 0
        for trade in rows.scalars().all():
            session.add(ShadowTrade(
                trade_id=trade.id,
                analysis_id=trade.analysis_id,
                ticker=trade.ticker,
                book=BOOK_PARALLEL,
                origin=ORIGIN_PARALLEL,
                entry_price=trade.entry_price,
                quantity=trade.quantity,
                expected_holding_days=trade.horizon_days or DEFAULT_HOLDING_DAYS,
                status="OPEN",
            ))
            opened += 1

        await session.flush()
        return opened

    async def _close_due(self, session: AsyncSession) -> int:
        now = utcnow_naive()
        rows = await session.execute(
            select(ShadowTrade).where(ShadowTrade.status == "OPEN")
        )
        due = [
            row for row in rows.scalars().all()
            if row.opened_at + timedelta(days=row.expected_holding_days) <= now
        ]
        if not due:
            return 0

        shadow_due = [row for row in due if row.book == BOOK_SHADOW]
        prices = await self._prices([row.ticker for row in shadow_due]) if shadow_due else {}

        closed = 0
        for row in due:
            if row.book == BOOK_SHADOW:
                price = prices.get(row.ticker.upper())
                if price is None or price <= 0:
                    continue
                pnl = (price - float(row.entry_price)) * row.quantity
                self._close_row(row, price, pnl, now)
                closed += 1
            else:
                closed += await self._close_parallel(session, row, now)

        await session.flush()
        return closed

    async def _close_parallel(
        self,
        session: AsyncSession,
        row: ShadowTrade,
        now,
    ) -> int:
        if self._broker is None or not self._broker.is_connected():
            return 0

        trade = await session.get(Trade, row.trade_id) if row.trade_id else None
        if trade is None:
            return 0

        contract = trade.contract_spec or {}
        mark = await self._mark_contract(contract)
        if mark is None:
            return 0

        multiplier = float(contract.get("multiplier") or 1)
        pnl = (mark - float(row.entry_price)) * row.quantity * multiplier
        self._close_row(row, mark, pnl, now)
        return 1

    async def _mark_contract(self, contract: dict) -> Optional[float]:
        net = 0.0
        for leg in contract.get("legs") or []:
            try:
                spec = InstrumentSpec.from_dict(leg["spec"])
                if spec.is_option:
                    quote = await self._broker.get_option_quote(spec)
                else:
                    quote = await self._broker.get_quote(spec)
            except Exception as e:
                self._logger.warning("parallel_mark_failed", error=str(e))
                return None

            mid = quote.mid
            if mid <= 0:
                return None
            net += mid if leg.get("side") == OrderSide.BUY.value else -mid
        return net if contract.get("legs") else None

    @staticmethod
    def _close_row(row: ShadowTrade, price: float, pnl: float, now) -> None:
        row.status = "CLOSED"
        row.closed_at = now
        row.close_price = Decimal(str(round(price, 4)))
        row.pnl_virtual = Decimal(str(round(pnl, 2)))
