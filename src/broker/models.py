from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


class InstrumentType(str, Enum):
    STOCK = "STOCK"
    OPTION = "OPTION"
    FUTURE = "FUTURE"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


@dataclass(frozen=True)
class InstrumentSpec:
    instrument: InstrumentType
    symbol: str
    currency: str = "USD"
    exchange: str = "SMART"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None
    multiplier: Optional[str] = None
    trading_class: Optional[str] = None
    local_symbol: Optional[str] = None
    con_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["instrument"] = self.instrument.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstrumentSpec":
        payload = dict(data)
        payload["instrument"] = InstrumentType(payload["instrument"])
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in allowed})

    @property
    def is_option(self) -> bool:
        return self.instrument is InstrumentType.OPTION

    @property
    def is_future(self) -> bool:
        return self.instrument is InstrumentType.FUTURE

    @property
    def contract_multiplier(self) -> float:
        if self.multiplier:
            return float(self.multiplier)
        return 1.0

    @property
    def expiry_date(self) -> Optional[date]:
        if not self.expiry:
            return None
        return datetime.strptime(self.expiry, "%Y%m%d").date()

    def describe(self) -> str:
        if self.is_option:
            return f"{self.symbol} {self.expiry} {self.strike}{self.right}"
        if self.is_future:
            return f"{self.symbol} {self.expiry}"
        return self.symbol


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int
    timestamp: datetime

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        if self.bid <= 0 or self.ask <= 0 or mid <= 0:
            return None
        return (self.ask - self.bid) / mid


@dataclass
class OptionQuote:
    spec: InstrumentSpec
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_vol: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]
    underlying_price: Optional[float]
    timestamp: datetime

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        if self.bid <= 0 or self.ask <= 0 or mid <= 0:
            return None
        return (self.ask - self.bid) / mid

    @property
    def premium_per_contract(self) -> float:
        return self.mid * self.spec.contract_multiplier

    def has_greeks(self) -> bool:
        return self.delta is not None and self.implied_vol is not None


@dataclass
class BrokerPosition:
    spec: InstrumentSpec
    quantity: int
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float

    @property
    def unrealized_pnl_pct(self) -> Optional[float]:
        basis = abs(self.avg_cost * self.quantity)
        if basis <= 0:
            return None
        return self.unrealized_pnl / basis


@dataclass
class AccountSummary:
    total_equity: float
    cash_balance: float
    buying_power: float
    unrealized_pnl: float
    maintenance_margin: float
    excess_liquidity: float


@dataclass
class MarginImpact:
    accepted: bool
    initial_margin_change: float
    maintenance_margin_change: float
    equity_with_loan_change: float
    commission: Optional[float] = None
    error: Optional[str] = None


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_quantity: int = 0
    status: OrderStatus = OrderStatus.PENDING
    commission: Optional[float] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class LiquidityCheck:
    passed: bool
    failures: list[str] = field(default_factory=list)
    spread_pct: Optional[float] = None
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    premium: Optional[float] = None

    def reason(self) -> str:
        return "; ".join(self.failures) if self.failures else "ok"
