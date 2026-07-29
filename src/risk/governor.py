from __future__ import annotations

import math

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import CapitalConfig, RiskConfig
from src.database.models import (
    DRAWDOWN_HALT,
    Analysis,
    CalibrationSnapshot,
    Trade,
)
from src.database.repositories.equity import (
    DRAWDOWN_MIN_CONVICTION,
    DRAWDOWN_SIZE_MULTIPLIER,
)
from src.database.repositories.errors import ErrorRepository
from src.database.repositories.trade import TradeRepository
from src.positions.models import BLOCKING_ERROR_TYPES
from src.risk.drawdown import DrawdownSnapshot, DrawdownTracker
from src.risk.kelly import conviction_bucket, payoff_odds, position_fraction
from src.risk.models import P_SOURCE_CALIBRATED, P_SOURCE_FALLBACK, RiskVerdict
from src.sentinel.universe import UniverseIndex, normalize_sector

INSTRUMENT_OPTION_KINDS = ("OPTION", "OPTION_SPREAD")

CHOICE_TO_INSTRUMENT = {
    "LONG_CALL": "OPTION",
    "LONG_PUT": "OPTION",
    "CALL_DEBIT_SPREAD": "OPTION_SPREAD",
    "PUT_DEBIT_SPREAD": "OPTION_SPREAD",
    "STOCK": "STOCK",
    "FUTURE": "FUTURE",
}


class RiskGovernor:

    def __init__(
        self,
        risk: RiskConfig,
        capital: CapitalConfig,
        universe: UniverseIndex,
    ) -> None:
        self._risk = risk
        self._capital = capital
        self._universe = universe
        self.tracker = DrawdownTracker(risk, capital.initial_usd)
        self._logger = structlog.get_logger("RiskGovernor")

    async def assess(
        self,
        session: AsyncSession,
        analysis: Analysis,
        snapshot: DrawdownSnapshot,
    ) -> RiskVerdict:
        contract = (analysis.structure_result or {}).get("contract") or {}
        choice = str(contract.get("choice") or "")
        instrument = CHOICE_TO_INSTRUMENT.get(choice)

        def veto(reason: str) -> RiskVerdict:
            return RiskVerdict(
                approved=False,
                drawdown_state=snapshot.state,
                veto_reason=reason,
            )

        if instrument is None:
            return veto(f"unknown instrument choice '{choice}'")

        if snapshot.state == DRAWDOWN_HALT:
            return veto("drawdown HALT — manual reset required (scripts/reset_halt.py)")

        blocking = await ErrorRepository(session).unresolved(BLOCKING_ERROR_TYPES)
        if blocking:
            return veto(
                f"blocked by unresolved critical error: {blocking[0].error_type} — "
                f"resolve it and run scripts/resolve_errors.py"
            )

        conviction = float(analysis.conviction or 0)
        floor = DRAWDOWN_MIN_CONVICTION[snapshot.state]
        if conviction < floor:
            return veto(
                f"conviction {conviction:.2f} below the {snapshot.state} floor {floor:.2f}"
            )

        trades = TradeRepository(session)
        if await trades.ticker_is_committed(analysis.ticker):
            return veto("ticker already has a committed position")

        committed = await trades.get_committed()

        if instrument == "FUTURE":
            futures_open = sum(1 for t in committed if t.instrument == "FUTURE")
            if futures_open >= self._risk.max_concurrent_futures:
                return veto(
                    f"futures limit reached ({futures_open}/{self._risk.max_concurrent_futures})"
                )

        if instrument in INSTRUMENT_OPTION_KINDS:
            options_open = sum(
                1 for t in committed if t.instrument in INSTRUMENT_OPTION_KINDS
            )
            if options_open >= self._risk.max_concurrent_options:
                return veto(
                    f"concurrent options limit reached "
                    f"({options_open}/{self._risk.max_concurrent_options})"
                )

        catalyst = (analysis.catalyst_type or "").upper()
        if catalyst:
            same_catalyst = await self._count_same_catalyst(session, committed, catalyst)
            if same_catalyst >= self._risk.max_same_catalyst_type:
                return veto(
                    f"catalyst limit reached for {catalyst} "
                    f"({same_catalyst}/{self._risk.max_same_catalyst_type})"
                )

        sector = self._sector(analysis.ticker)
        same_sector = sum(1 for t in committed if self._sector(t.ticker) == sector)
        if same_sector >= self._risk.max_same_sector:
            return veto(
                f"sector limit reached for {sector} "
                f"({same_sector}/{self._risk.max_same_sector})"
            )

        net_debit = float(contract.get("net_debit_per_unit") or 0)
        max_loss = float(contract.get("max_loss_per_unit") or net_debit)
        if max_loss <= 0:
            return veto("max loss per unit is not positive — cannot size the position")

        p, p_source = await self._hit_rate(session, catalyst, conviction)
        kelly_multiplier = self._risk.kelly_fraction
        if p_source == P_SOURCE_FALLBACK:
            kelly_multiplier /= 2.0

        strikes = [
            leg.get("spec", {}).get("strike")
            for leg in (contract.get("legs") or [])
        ]
        b = payoff_odds(
            choice=choice,
            target_move_pct=float(analysis.expected_move_pct or 0),
            net_debit=net_debit,
            max_loss=max_loss,
            underlying_price=contract.get("underlying_price"),
            strikes=[s for s in strikes if s],
        )

        fraction = position_fraction(p, b, kelly_multiplier, self._risk.max_position_pct)
        fraction *= DRAWDOWN_SIZE_MULTIPLIER[snapshot.state]
        if fraction <= 0:
            return RiskVerdict(
                approved=False,
                drawdown_state=snapshot.state,
                veto_reason=(
                    f"kelly sizing non-positive (p={p:.2f}, b={b:.2f}) — "
                    f"payoff does not clear the measured edge"
                ),
                hit_rate_used=p,
                payoff_odds_used=b,
                hit_rate_source=p_source,
            )

        quantity = math.floor(fraction * snapshot.equity / max_loss)

        open_risk = float(await trades.total_capital_at_risk())
        if instrument in INSTRUMENT_OPTION_KINDS:
            premium_cap = self._risk.max_total_premium_pct * snapshot.equity
            headroom = premium_cap - open_risk
            quantity = min(quantity, math.floor(headroom / max_loss) if headroom > 0 else 0)
        else:
            bucket_cap = self._capital.option_bucket_pct * snapshot.equity
            headroom = bucket_cap - open_risk
            unit_cost = net_debit if net_debit > 0 else max_loss
            quantity = min(quantity, math.floor(headroom / unit_cost) if headroom > 0 else 0)

        if quantity < 1:
            return RiskVerdict(
                approved=False,
                drawdown_state=snapshot.state,
                veto_reason="position below minimum size at the current risk budget",
                hit_rate_used=p,
                payoff_odds_used=b,
                hit_rate_source=p_source,
            )

        return RiskVerdict(
            approved=True,
            quantity=quantity,
            capital_at_risk=quantity * max_loss,
            kelly_fraction_used=fraction,
            drawdown_state=snapshot.state,
            hit_rate_used=p,
            payoff_odds_used=b,
            hit_rate_source=p_source,
        )

    async def _hit_rate(
        self,
        session: AsyncSession,
        catalyst: str,
        conviction: float,
    ) -> tuple[float, str]:
        if catalyst:
            result = await session.execute(
                select(CalibrationSnapshot)
                .where(
                    CalibrationSnapshot.catalyst_type == catalyst,
                    CalibrationSnapshot.conviction_bucket == conviction_bucket(conviction),
                )
                .order_by(CalibrationSnapshot.as_of.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if (
                row is not None
                and row.hit_rate is not None
                and row.sample_size >= self._risk.min_calibration_n
            ):
                return float(row.hit_rate), P_SOURCE_CALIBRATED
        return self._risk.fallback_hit_rate, P_SOURCE_FALLBACK

    async def _count_same_catalyst(
        self,
        session: AsyncSession,
        committed: list[Trade],
        catalyst: str,
    ) -> int:
        ids = [t.analysis_id for t in committed if t.analysis_id is not None]
        if not ids:
            return 0
        result = await session.execute(
            select(Analysis.catalyst_type).where(Analysis.id.in_(ids))
        )
        return sum(
            1 for value in result.scalars().all()
            if (value or "").upper() == catalyst
        )

    def _sector(self, ticker: str) -> str:
        entry = self._universe.get(ticker)
        return normalize_sector(entry.sector if entry else None)
