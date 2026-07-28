from __future__ import annotations

from datetime import date, datetime, timedelta

from ib_async import Contract, Future, Option, Stock

from src.broker.models import InstrumentSpec, InstrumentType, OptionRight
from src.core.exceptions import ContractResolutionError

BFF_SYMBOL = "BFF"
BFF_EXCHANGE = "CME"
BFF_MULTIPLIER_BTC = 0.02

MBT_SYMBOL = "MBT"
MET_SYMBOL = "MET"

CRYPTO_FUTURE_SYMBOLS = (BFF_SYMBOL, MBT_SYMBOL, MET_SYMBOL)

DATE_FORMAT = "%Y%m%d"


def format_expiry(value: date | datetime | str) -> str:
    if isinstance(value, str):
        cleaned = value.replace("-", "")
        if len(cleaned) not in (6, 8):
            raise ContractResolutionError(f"Unsupported expiry format: {value}")
        return cleaned
    return value.strftime(DATE_FORMAT)


def parse_expiry(value: str) -> date:
    cleaned = value.replace("-", "")
    if len(cleaned) == 6:
        return datetime.strptime(cleaned + "01", DATE_FORMAT).date()
    return datetime.strptime(cleaned, DATE_FORMAT).date()


def days_to_expiry(spec: InstrumentSpec, reference: date | None = None) -> int | None:
    if not spec.expiry:
        return None
    reference = reference or datetime.utcnow().date()
    return (parse_expiry(spec.expiry) - reference).days


def stock_spec(symbol: str, exchange: str = "SMART", currency: str = "USD") -> InstrumentSpec:
    return InstrumentSpec(
        instrument=InstrumentType.STOCK,
        symbol=symbol.upper(),
        exchange=exchange,
        currency=currency,
    )


def option_spec(
    symbol: str,
    expiry: date | datetime | str,
    strike: float,
    right: OptionRight | str,
    exchange: str = "SMART",
    currency: str = "USD",
    trading_class: str | None = None,
) -> InstrumentSpec:
    right_value = right.value if isinstance(right, OptionRight) else right.upper()[0]
    if right_value not in ("C", "P"):
        raise ContractResolutionError(f"Invalid option right: {right}")

    return InstrumentSpec(
        instrument=InstrumentType.OPTION,
        symbol=symbol.upper(),
        expiry=format_expiry(expiry),
        strike=float(strike),
        right=right_value,
        exchange=exchange,
        currency=currency,
        multiplier="100",
        trading_class=trading_class,
    )


def future_spec(
    symbol: str,
    expiry: date | datetime | str,
    exchange: str = BFF_EXCHANGE,
    currency: str = "USD",
    local_symbol: str | None = None,
) -> InstrumentSpec:
    return InstrumentSpec(
        instrument=InstrumentType.FUTURE,
        symbol=symbol.upper(),
        expiry=format_expiry(expiry),
        exchange=exchange,
        currency=currency,
        local_symbol=local_symbol,
    )


def to_ib_contract(spec: InstrumentSpec) -> Contract:
    if spec.instrument is InstrumentType.STOCK:
        return Stock(spec.symbol, spec.exchange, spec.currency)

    if spec.instrument is InstrumentType.OPTION:
        if not spec.expiry or spec.strike is None or not spec.right:
            raise ContractResolutionError(
                f"Option spec is incomplete: {spec.describe()}"
            )
        contract = Option(
            spec.symbol,
            spec.expiry,
            spec.strike,
            spec.right,
            spec.exchange,
            currency=spec.currency,
            multiplier=spec.multiplier or "100",
        )
        if spec.trading_class:
            contract.tradingClass = spec.trading_class
        return contract

    if spec.instrument is InstrumentType.FUTURE:
        if not spec.expiry:
            raise ContractResolutionError(
                f"Future spec is missing expiry: {spec.describe()}"
            )
        contract = Future(
            spec.symbol,
            spec.expiry,
            spec.exchange,
            currency=spec.currency,
        )
        if spec.local_symbol:
            contract.localSymbol = spec.local_symbol
        return contract

    raise ContractResolutionError(f"Unsupported instrument: {spec.instrument}")


def from_ib_contract(contract: Contract) -> InstrumentSpec:
    sec_type = contract.secType

    if sec_type == "STK":
        instrument = InstrumentType.STOCK
    elif sec_type == "OPT":
        instrument = InstrumentType.OPTION
    elif sec_type in ("FUT", "CONTFUT"):
        instrument = InstrumentType.FUTURE
    else:
        raise ContractResolutionError(f"Unsupported IB secType: {sec_type}")

    return InstrumentSpec(
        instrument=instrument,
        symbol=contract.symbol,
        currency=contract.currency or "USD",
        exchange=contract.exchange or "SMART",
        expiry=contract.lastTradeDateOrContractMonth or None,
        strike=float(contract.strike) if contract.strike else None,
        right=contract.right or None,
        multiplier=str(contract.multiplier) if contract.multiplier else None,
        trading_class=contract.tradingClass or None,
        local_symbol=contract.localSymbol or None,
        con_id=contract.conId or None,
    )


def select_expiry(
    available: list[str],
    not_before: date,
    min_days: int,
    reference: date | None = None,
) -> str | None:
    reference = reference or datetime.utcnow().date()
    floor = max(not_before, reference + timedelta(days=min_days))

    candidates = sorted(
        (e for e in available if parse_expiry(e) >= floor),
        key=parse_expiry,
    )
    return candidates[0] if candidates else None


def next_bff_expiry(reference: date | None = None, min_days: int = 2) -> date:
    reference = reference or datetime.utcnow().date()
    floor = reference + timedelta(days=min_days)
    days_ahead = (4 - floor.weekday()) % 7
    return floor + timedelta(days=days_ahead)
