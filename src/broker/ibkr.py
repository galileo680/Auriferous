from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Any, Optional

import structlog
from ib_async import IB, LimitOrder, StopOrder, Trade

from src.broker.contracts import from_ib_contract, to_ib_contract
from src.broker.interface import BrokerInterface
from src.broker.models import (
    AccountSummary,
    BrokerPosition,
    InstrumentSpec,
    InstrumentType,
    MarginImpact,
    OptionQuote,
    OrderResult,
    OrderSide,
    OrderStatus,
    Quote,
)
from src.core.clock import utcnow
from src.core.config import BrokerConfig
from src.core.exceptions import (
    BrokerConnectionError,
    ContractResolutionError,
    OrderError,
)

QUOTE_POLL_INTERVAL = 0.2
QUOTE_POLL_ATTEMPTS = 15
GREEKS_POLL_ATTEMPTS = 25
ACCOUNT_CACHE_TTL = 10
POSITIONS_CACHE_TTL = 5
OPTION_GENERIC_TICKS = "100,101,106"

IB_STATUS_MAP = {
    "PendingSubmit": OrderStatus.PENDING,
    "ApiPending": OrderStatus.PENDING,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


class IBKRClient(BrokerInterface):

    def __init__(self, config: BrokerConfig) -> None:
        self._config = config
        self._ib = IB()
        self._logger = structlog.get_logger("IBKRClient")

        self._connected = False
        self._account: Optional[str] = None
        self._base_currency = "USD"

        self._account_cache: Optional[AccountSummary] = None
        self._account_cache_at: Optional[datetime] = None
        self._positions_cache: Optional[list[BrokerPosition]] = None
        self._positions_cache_at: Optional[datetime] = None

        self._chain_cache: dict[str, list[Any]] = {}

    @property
    def ib(self) -> IB:
        return self._ib

    async def connect(self) -> bool:
        if self._connected and self._ib.isConnected():
            return True

        try:
            await self._ib.connectAsync(
                host=self._config.host,
                port=self._config.port,
                clientId=self._config.client_id,
                timeout=self._config.timeout_seconds,
                readonly=self._config.readonly,
            )
        except ConnectionRefusedError:
            raise BrokerConnectionError(
                f"Connection refused — is IB Gateway running on "
                f"{self._config.host}:{self._config.port}?"
            )
        except asyncio.TimeoutError:
            raise BrokerConnectionError(
                f"Connection timed out after {self._config.timeout_seconds}s"
            )

        accounts = self._ib.managedAccounts()
        if not accounts:
            raise BrokerConnectionError("No managed accounts returned by IB Gateway")

        self._account = accounts[0]
        self._ib.client.reqAccountUpdates(True, self._account)
        await asyncio.sleep(1)

        for value in self._ib.accountValues(self._account):
            if value.currency and value.currency not in ("BASE", ""):
                self._base_currency = value.currency
                break

        self._ib.reqMarketDataType(self._config.market_data_type)
        self._connected = True

        self._logger.info(
            "broker_connected",
            account=self._account,
            client_id=self._config.client_id,
            base_currency=self._base_currency,
            market_data_type=self._config.market_data_type,
        )
        return True

    async def disconnect(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()
        self._connected = False
        self._logger.info("broker_disconnected")

    def is_connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise BrokerConnectionError("Not connected to IB Gateway")

    def _cache_valid(self, stamp: Optional[datetime], ttl: int) -> bool:
        if stamp is None:
            return False
        return (utcnow() - stamp).total_seconds() < ttl

    async def qualify(self, spec: InstrumentSpec) -> InstrumentSpec:
        self._ensure_connected()

        contract = to_ib_contract(spec)
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ContractResolutionError(
                f"IB could not qualify contract: {spec.describe()}"
            )
        return from_ib_contract(qualified[0])

    async def get_account_summary(self, force_refresh: bool = False) -> AccountSummary:
        self._ensure_connected()

        if not force_refresh and self._cache_valid(self._account_cache_at, ACCOUNT_CACHE_TTL):
            return self._account_cache

        lookup: dict[str, str] = {}
        for value in self._ib.accountValues(self._account):
            if value.currency in (self._base_currency, "BASE", ""):
                lookup[value.tag] = value.value

        summary = AccountSummary(
            total_equity=_safe_float(lookup.get("NetLiquidation"), 0.0),
            cash_balance=_safe_float(lookup.get("TotalCashValue"), 0.0),
            buying_power=_safe_float(lookup.get("BuyingPower"), 0.0),
            unrealized_pnl=_safe_float(lookup.get("UnrealizedPnL"), 0.0),
            maintenance_margin=_safe_float(lookup.get("MaintMarginReq"), 0.0),
            excess_liquidity=_safe_float(lookup.get("ExcessLiquidity"), 0.0),
        )

        self._account_cache = summary
        self._account_cache_at = utcnow()
        return summary

    async def get_positions(self, force_refresh: bool = False) -> list[BrokerPosition]:
        self._ensure_connected()

        if not force_refresh and self._cache_valid(self._positions_cache_at, POSITIONS_CACHE_TTL):
            return self._positions_cache

        positions: list[BrokerPosition] = []
        for item in self._ib.portfolio(self._account):
            try:
                spec = from_ib_contract(item.contract)
            except ContractResolutionError:
                continue

            positions.append(BrokerPosition(
                spec=spec,
                quantity=int(item.position),
                avg_cost=_safe_float(item.averageCost, 0.0),
                market_price=_safe_float(item.marketPrice, 0.0),
                market_value=_safe_float(item.marketValue, 0.0),
                unrealized_pnl=_safe_float(item.unrealizedPNL, 0.0),
            ))

        self._positions_cache = positions
        self._positions_cache_at = utcnow()
        return positions

    async def get_quote(self, spec: InstrumentSpec) -> Quote:
        self._ensure_connected()

        contract = to_ib_contract(spec)
        await self._ib.qualifyContractsAsync(contract)
        ticker = self._ib.reqMktData(contract, snapshot=False)

        try:
            for _ in range(QUOTE_POLL_ATTEMPTS):
                await asyncio.sleep(QUOTE_POLL_INTERVAL)
                if _safe_float(ticker.last) or _safe_float(ticker.close):
                    break

            last = _safe_float(ticker.last) or _safe_float(ticker.close) or 0.0
            return Quote(
                symbol=spec.symbol,
                bid=_safe_float(ticker.bid, 0.0),
                ask=_safe_float(ticker.ask, 0.0),
                last=last,
                volume=int(_safe_float(ticker.volume, 0.0) or 0),
                timestamp=utcnow(),
            )
        finally:
            self._ib.cancelMktData(contract)

    async def get_quotes(self, specs: list[InstrumentSpec]) -> dict[str, Quote]:
        self._ensure_connected()
        if not specs:
            return {}

        contracts = [to_ib_contract(s) for s in specs]
        await self._ib.qualifyContractsAsync(*contracts)

        tickers = {
            spec.symbol: self._ib.reqMktData(contract, snapshot=False)
            for spec, contract in zip(specs, contracts)
        }

        try:
            for _ in range(QUOTE_POLL_ATTEMPTS):
                await asyncio.sleep(QUOTE_POLL_INTERVAL)
                if all(
                    _safe_float(t.last) or _safe_float(t.close)
                    for t in tickers.values()
                ):
                    break

            result: dict[str, Quote] = {}
            for symbol, ticker in tickers.items():
                last = _safe_float(ticker.last) or _safe_float(ticker.close) or 0.0
                result[symbol] = Quote(
                    symbol=symbol,
                    bid=_safe_float(ticker.bid, 0.0),
                    ask=_safe_float(ticker.ask, 0.0),
                    last=last,
                    volume=int(_safe_float(ticker.volume, 0.0) or 0),
                    timestamp=utcnow(),
                )
            return result
        finally:
            for contract in contracts:
                self._ib.cancelMktData(contract)

    async def get_option_quote(self, spec: InstrumentSpec) -> OptionQuote:
        self._ensure_connected()
        if not spec.is_option:
            raise ContractResolutionError(
                f"get_option_quote requires an option spec, got {spec.instrument}"
            )

        contract = to_ib_contract(spec)
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ContractResolutionError(f"Could not qualify option {spec.describe()}")

        ticker = self._ib.reqMktData(contract, genericTickList=OPTION_GENERIC_TICKS, snapshot=False)

        try:
            greeks = None
            for _ in range(GREEKS_POLL_ATTEMPTS):
                await asyncio.sleep(QUOTE_POLL_INTERVAL)
                greeks = ticker.modelGreeks or ticker.lastGreeks or ticker.midGreeks
                if greeks is not None and _safe_float(ticker.bid) is not None:
                    break

            open_interest = _safe_float(
                ticker.callOpenInterest if spec.right == "C" else ticker.putOpenInterest,
                0.0,
            )

            return OptionQuote(
                spec=from_ib_contract(qualified[0]),
                bid=_safe_float(ticker.bid, 0.0),
                ask=_safe_float(ticker.ask, 0.0),
                last=_safe_float(ticker.last, 0.0) or _safe_float(ticker.close, 0.0),
                volume=int(_safe_float(ticker.volume, 0.0) or 0),
                open_interest=int(open_interest or 0),
                implied_vol=_safe_float(greeks.impliedVol) if greeks else None,
                delta=_safe_float(greeks.delta) if greeks else None,
                gamma=_safe_float(greeks.gamma) if greeks else None,
                theta=_safe_float(greeks.theta) if greeks else None,
                vega=_safe_float(greeks.vega) if greeks else None,
                underlying_price=_safe_float(greeks.undPrice) if greeks else None,
                timestamp=utcnow(),
            )
        finally:
            self._ib.cancelMktData(contract)

    async def _option_params(self, symbol: str) -> list[Any]:
        if symbol in self._chain_cache:
            return self._chain_cache[symbol]

        from src.broker.contracts import stock_spec

        underlying = to_ib_contract(stock_spec(symbol))
        qualified = await self._ib.qualifyContractsAsync(underlying)
        if not qualified:
            raise ContractResolutionError(f"Could not qualify underlying {symbol}")

        params = await self._ib.reqSecDefOptParamsAsync(
            underlyingSymbol=symbol.upper(),
            futFopExchange="",
            underlyingSecType="STK",
            underlyingConId=qualified[0].conId,
        )
        self._chain_cache[symbol] = list(params)
        return self._chain_cache[symbol]

    async def get_option_expirations(self, symbol: str) -> list[str]:
        self._ensure_connected()
        params = await self._option_params(symbol)

        expirations: set[str] = set()
        for entry in params:
            if entry.exchange == "SMART":
                expirations.update(entry.expirations)
        if not expirations:
            for entry in params:
                expirations.update(entry.expirations)

        return sorted(expirations)

    async def get_option_strikes(self, symbol: str, expiry: str) -> list[float]:
        self._ensure_connected()
        params = await self._option_params(symbol)

        strikes: set[float] = set()
        for entry in params:
            if expiry in entry.expirations:
                strikes.update(float(s) for s in entry.strikes)

        return sorted(strikes)

    async def get_future_expirations(self, symbol: str, exchange: str) -> list[str]:
        self._ensure_connected()

        from ib_async import Future

        template = Future(symbol=symbol.upper(), exchange=exchange, currency="USD")
        details = await self._ib.reqContractDetailsAsync(template)
        if not details:
            raise ContractResolutionError(
                f"No future contracts found for {symbol} on {exchange}"
            )

        return sorted({d.contract.lastTradeDateOrContractMonth for d in details})

    async def check_margin(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
        limit_price: float,
    ) -> MarginImpact:
        self._ensure_connected()

        contract = to_ib_contract(spec)
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            return MarginImpact(False, 0.0, 0.0, 0.0, error="contract not qualified")

        order = LimitOrder(side.value, quantity, limit_price)
        order.transmit = False

        try:
            state = await self._ib.whatIfOrderAsync(qualified[0], order)
        except Exception as e:
            return MarginImpact(False, 0.0, 0.0, 0.0, error=str(e))

        if state is None:
            return MarginImpact(False, 0.0, 0.0, 0.0, error="whatIf returned nothing")

        return MarginImpact(
            accepted=True,
            initial_margin_change=_safe_float(state.initMarginChange, 0.0),
            maintenance_margin_change=_safe_float(state.maintMarginChange, 0.0),
            equity_with_loan_change=_safe_float(state.equityWithLoanChange, 0.0),
            commission=_safe_float(state.commission),
        )

    async def place_limit_order(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
        limit_price: float,
        transmit: bool = True,
    ) -> OrderResult:
        self._ensure_connected()
        if quantity <= 0:
            raise OrderError(f"Invalid quantity {quantity} for {spec.describe()}")

        try:
            contract = to_ib_contract(spec)
            await self._ib.qualifyContractsAsync(contract)

            order = LimitOrder(side.value, quantity, round(limit_price, 4))
            order.transmit = transmit
            trade = self._ib.placeOrder(contract, order)

            self._logger.info(
                "limit_order_placed",
                instrument=spec.describe(),
                side=side.value,
                quantity=quantity,
                limit_price=limit_price,
                order_id=trade.order.orderId,
            )
            return OrderResult(
                success=True,
                order_id=str(trade.order.orderId),
                status=OrderStatus.SUBMITTED,
            )
        except Exception as e:
            self._logger.error(
                "limit_order_failed", instrument=spec.describe(), error=str(e)
            )
            raise OrderError(f"Limit order failed for {spec.describe()}: {e}")

    async def place_stop_order(
        self,
        spec: InstrumentSpec,
        side: OrderSide,
        quantity: int,
        stop_price: float,
    ) -> OrderResult:
        self._ensure_connected()
        if spec.instrument is InstrumentType.OPTION:
            raise OrderError(
                "Stop orders on options are not used — premium stops are managed in-process"
            )

        try:
            contract = to_ib_contract(spec)
            await self._ib.qualifyContractsAsync(contract)

            order = StopOrder(side.value, quantity, round(stop_price, 4))
            trade = self._ib.placeOrder(contract, order)

            self._logger.info(
                "stop_order_placed",
                instrument=spec.describe(),
                side=side.value,
                quantity=quantity,
                stop_price=stop_price,
                order_id=trade.order.orderId,
            )
            return OrderResult(
                success=True,
                order_id=str(trade.order.orderId),
                status=OrderStatus.SUBMITTED,
            )
        except Exception as e:
            self._logger.error(
                "stop_order_failed", instrument=spec.describe(), error=str(e)
            )
            raise OrderError(f"Stop order failed for {spec.describe()}: {e}")

    def _find_trade(self, order_id: str) -> Optional[Trade]:
        if not order_id:
            return None
        try:
            oid = int(order_id)
        except ValueError:
            return None
        for trade in self._ib.trades():
            if trade.order.orderId == oid or trade.order.permId == oid:
                return trade
        return None

    async def modify_order(self, order_id: str, new_price: float) -> OrderResult:
        self._ensure_connected()

        trade = self._find_trade(order_id)
        if trade is None:
            return OrderResult(success=False, order_id=order_id, error="order not found")

        order = trade.order
        if order.orderType == "LMT":
            order.lmtPrice = round(new_price, 4)
        elif order.orderType == "STP":
            order.auxPrice = round(new_price, 4)
        else:
            return OrderResult(
                success=False, order_id=order_id, error=f"cannot modify {order.orderType}"
            )

        updated = self._ib.placeOrder(trade.contract, order)
        self._logger.info("order_modified", order_id=order_id, new_price=new_price)
        return OrderResult(
            success=True,
            order_id=str(updated.order.orderId),
            status=IB_STATUS_MAP.get(updated.orderStatus.status, OrderStatus.PENDING),
        )

    async def cancel_order(self, order_id: str) -> bool:
        self._ensure_connected()

        trade = self._find_trade(order_id)
        if trade is None:
            return False

        self._ib.cancelOrder(trade.order)
        self._logger.info("order_cancelled", order_id=order_id)
        return True

    async def get_order_status(self, order_id: str) -> OrderStatus:
        self._ensure_connected()

        trade = self._find_trade(order_id)
        if trade is None:
            return OrderStatus.EXPIRED
        return IB_STATUS_MAP.get(trade.orderStatus.status, OrderStatus.PENDING)
