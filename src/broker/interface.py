from __future__ import annotations

from abc import ABC, abstractmethod

from src.broker.models import (
    AccountSummary,
    BrokerPosition,
    InstrumentSpec,
    MarginImpact,
    OptionQuote,
    OrderResult,
    OrderSide,
    OrderStatus,
    Quote,
)


class BrokerInterface(ABC):

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        ...

    @abstractmethod
    async def qualify(self, spec: InstrumentSpec) -> InstrumentSpec:
        ...

    @abstractmethod
    async def get_quote(self, spec: InstrumentSpec) -> Quote:
        ...

    @abstractmethod
    async def get_quotes(self, specs: list[InstrumentSpec]) -> dict[str, Quote]:
        ...

    @abstractmethod
    async def get_option_quote(self, spec: InstrumentSpec) -> OptionQuote:
        ...

    @abstractmethod
    async def get_option_expirations(self, symbol: str) -> list[str]:
        ...

    @abstractmethod
    async def get_option_strikes(self, symbol: str, expiry: str) -> list[float]:
        ...

    @abstractmethod
    async def get_future_expirations(self, symbol: str, exchange: str) -> list[str]:
        ...

    @abstractmethod
    async def check_margin(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
        limit_price: float,
    ) -> MarginImpact:
        ...

    @abstractmethod
    async def place_limit_order(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
        limit_price: float,
        transmit: bool = True,
    ) -> OrderResult:
        ...

    @abstractmethod
    async def place_stop_order(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
        stop_price: float,
    ) -> OrderResult:
        ...

    @abstractmethod
    async def modify_order(self, order_id: str, new_price: float) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus:
        ...
