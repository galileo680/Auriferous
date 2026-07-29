from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from src.broker.models import InstrumentSpec, OrderSide

BINARY_CATALYSTS = frozenset({
    "FDA_DECISION",
    "MA",
    "LEGAL",
    "EARNINGS_SURPRISE",
    "ACCOUNTING_RED_FLAG",
})

NICHE_CATALYSTS = frozenset({
    "ACCOUNTING_RED_FLAG",
    "LEGAL",
    "MGMT_CHANGE",
})


class InstrumentChoice(str, Enum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    CALL_DEBIT_SPREAD = "CALL_DEBIT_SPREAD"
    PUT_DEBIT_SPREAD = "PUT_DEBIT_SPREAD"
    STOCK = "STOCK"
    FUTURE = "FUTURE"
    SKIP = "SKIP"

    @property
    def is_option(self) -> bool:
        return self in (
            InstrumentChoice.LONG_CALL,
            InstrumentChoice.LONG_PUT,
            InstrumentChoice.CALL_DEBIT_SPREAD,
            InstrumentChoice.PUT_DEBIT_SPREAD,
        )

    @property
    def is_spread(self) -> bool:
        return self in (
            InstrumentChoice.CALL_DEBIT_SPREAD,
            InstrumentChoice.PUT_DEBIT_SPREAD,
        )

    @property
    def is_long_premium(self) -> bool:
        return self in (InstrumentChoice.LONG_CALL, InstrumentChoice.LONG_PUT)


class StructureOutcome(str, Enum):
    STRUCTURED = "STRUCTURED"
    SKIP_HIGH_IV = "SKIP_HIGH_IV"
    SKIP_NO_CONTRACT = "SKIP_NO_CONTRACT"
    SKIP_ILLIQUID = "SKIP_ILLIQUID"
    SKIP_TOO_EXPENSIVE = "SKIP_TOO_EXPENSIVE"
    SKIP_NO_PRICE = "SKIP_NO_PRICE"
    SKIP_ERROR = "SKIP_ERROR"

    @property
    def is_skip(self) -> bool:
        return self is not StructureOutcome.STRUCTURED


@dataclass
class IVProfile:
    iv_current: Optional[float] = None
    iv_rank: Optional[float] = None
    realized_vol: Optional[float] = None
    sample_size: int = 0
    source: str = "unavailable"

    @property
    def iv_vs_realized(self) -> Optional[float]:
        if self.iv_current is None or not self.realized_vol:
            return None
        return self.iv_current / self.realized_vol

    def to_dict(self) -> dict[str, Any]:
        return {
            "iv_current": self.iv_current,
            "iv_rank": self.iv_rank,
            "realized_vol": self.realized_vol,
            "iv_vs_realized": self.iv_vs_realized,
            "sample_size": self.sample_size,
            "source": self.source,
        }


@dataclass
class TradeLeg:
    spec: InstrumentSpec
    side: OrderSide
    ratio: int = 1
    limit_price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "side": self.side.value,
            "ratio": self.ratio,
            "limit_price": self.limit_price,
        }


@dataclass
class InstrumentDecision:
    choice: InstrumentChoice
    reason: str
    binary_event: bool = False


@dataclass
class StructuredTrade:
    ticker: str
    market: str
    choice: InstrumentChoice
    direction: str
    legs: list[TradeLeg]
    net_debit_per_unit: float
    max_loss_per_unit: float
    target_move_pct: float
    horizon_days: int
    conviction: float
    iv: IVProfile = field(default_factory=IVProfile)
    underlying_price: Optional[float] = None
    invalidation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def primary_leg(self) -> TradeLeg:
        return self.legs[0]

    @property
    def instrument_kind(self) -> str:
        if self.choice.is_spread:
            return "OPTION_SPREAD"
        if self.choice.is_option:
            return "OPTION"
        if self.choice is InstrumentChoice.FUTURE:
            return "FUTURE"
        return "STOCK"

    def contract_payload(self) -> dict[str, Any]:
        return {
            "choice": self.choice.value,
            "legs": [leg.to_dict() for leg in self.legs],
            "net_debit_per_unit": self.net_debit_per_unit,
            "max_loss_per_unit": self.max_loss_per_unit,
            "underlying_price": self.underlying_price,
            "target_move_pct": self.target_move_pct,
            "horizon_days": self.horizon_days,
            "invalidation": self.invalidation,
            "iv": self.iv.to_dict(),
            "notes": self.notes,
        }


@dataclass
class StructureResult:
    outcome: StructureOutcome
    trade: Optional[StructuredTrade] = None
    decision: Optional[InstrumentDecision] = None
    reason: str = ""

    @property
    def structured(self) -> bool:
        return self.outcome is StructureOutcome.STRUCTURED

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            "reason": self.reason,
        }
        if self.decision is not None:
            payload["choice"] = self.decision.choice.value
            payload["choice_reason"] = self.decision.reason
            payload["binary_event"] = self.decision.binary_event
        if self.trade is not None:
            payload["contract"] = self.trade.contract_payload()
        return payload
