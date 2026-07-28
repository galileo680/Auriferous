from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import structlog

from src.broker.contracts import (
    BFF_EXCHANGE,
    BFF_MULTIPLIER_BTC,
    BFF_SYMBOL,
    future_spec,
    parse_expiry,
)
from src.broker.interface import BrokerInterface
from src.broker.models import InstrumentSpec, MarginImpact, OrderSide
from src.core.exceptions import ContractResolutionError

MIN_DAYS_TO_FUTURE_EXPIRY = 2
MBT_MIN_EQUITY_USD = 15_000.0


@dataclass
class FutureCandidate:
    spec: InstrumentSpec
    margin: MarginImpact
    days_to_expiry: int

    @property
    def initial_margin(self) -> float:
        return abs(self.margin.initial_margin_change)


class FutureSelector:

    def __init__(self, broker: BrokerInterface) -> None:
        self._broker = broker
        self._logger = structlog.get_logger("FutureSelector")

    async def find_bff(
        self,
        horizon_days: int,
        available_capital: float,
        reference_price: float,
        reference: date | None = None,
    ) -> FutureCandidate | None:
        reference = reference or date.today()
        floor = reference + timedelta(
            days=max(horizon_days, MIN_DAYS_TO_FUTURE_EXPIRY)
        )

        expirations = await self._broker.get_future_expirations(BFF_SYMBOL, BFF_EXCHANGE)
        candidates = sorted(
            (e for e in expirations if parse_expiry(e) >= floor),
            key=parse_expiry,
        )
        if not candidates:
            self._logger.info(
                "bff_no_expiry",
                horizon_days=horizon_days,
                available=len(expirations),
            )
            return None

        expiry = candidates[0]
        spec = await self._broker.qualify(future_spec(BFF_SYMBOL, expiry, BFF_EXCHANGE))

        margin = await self._broker.check_margin(
            spec=spec,
            side=OrderSide.BUY,
            quantity=1,
            limit_price=reference_price,
        )
        if not margin.accepted:
            self._logger.warning("bff_margin_check_failed", error=margin.error)
            return None

        required = abs(margin.initial_margin_change)
        if required > available_capital:
            self._logger.info(
                "bff_insufficient_capital",
                required=round(required, 2),
                available=round(available_capital, 2),
            )
            return None

        candidate = FutureCandidate(
            spec=spec,
            margin=margin,
            days_to_expiry=(parse_expiry(expiry) - reference).days,
        )
        self._logger.info(
            "bff_selected",
            contract=spec.describe(),
            initial_margin=round(required, 2),
            days_to_expiry=candidate.days_to_expiry,
            btc_exposure=BFF_MULTIPLIER_BTC,
        )
        return candidate

    def max_contracts(self, available_capital: float, margin_per_contract: float) -> int:
        if margin_per_contract <= 0:
            raise ContractResolutionError("margin per contract must be positive")
        return int(available_capital // margin_per_contract)


def needs_roll(
    spec: InstrumentSpec,
    roll_weekday: int,
    roll_hour_utc: int,
    now: date,
    now_hour_utc: int,
    now_weekday: int,
) -> bool:
    expiry = parse_expiry(spec.expiry) if spec.expiry else None
    if expiry is None:
        return False
    if expiry <= now:
        return True
    if now_weekday != roll_weekday or now_hour_utc < roll_hour_utc:
        return False
    return (expiry - now).days <= 1
