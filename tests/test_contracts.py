from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.contracts import (
    BFF_EXCHANGE,
    BFF_SYMBOL,
    days_to_expiry,
    format_expiry,
    future_spec,
    next_bff_expiry,
    option_spec,
    parse_expiry,
    select_expiry,
    stock_spec,
    to_ib_contract,
)
from src.broker.models import InstrumentSpec, InstrumentType, OptionRight
from src.core.exceptions import ContractResolutionError


def test_stock_spec_uppercases_and_defaults():
    spec = stock_spec("amd")
    assert spec.symbol == "AMD"
    assert spec.instrument is InstrumentType.STOCK
    assert spec.exchange == "SMART"
    assert spec.contract_multiplier == 1.0


def test_option_spec_sets_multiplier_and_right():
    spec = option_spec("AMD", date(2026, 8, 21), 150.0, OptionRight.CALL)
    assert spec.expiry == "20260821"
    assert spec.right == "C"
    assert spec.multiplier == "100"
    assert spec.contract_multiplier == 100.0
    assert spec.describe() == "AMD 20260821 150.0C"


def test_option_spec_rejects_invalid_right():
    with pytest.raises(ContractResolutionError):
        option_spec("AMD", "20260821", 150.0, "X")


def test_format_expiry_accepts_dashes_and_dates():
    assert format_expiry("2026-08-21") == "20260821"
    assert format_expiry(date(2026, 8, 21)) == "20260821"


def test_format_expiry_rejects_garbage():
    with pytest.raises(ContractResolutionError):
        format_expiry("21 sierpnia")


def test_parse_expiry_handles_monthly_contracts():
    assert parse_expiry("202608") == date(2026, 8, 1)
    assert parse_expiry("20260821") == date(2026, 8, 21)


def test_to_ib_contract_builds_option():
    spec = option_spec("AMD", "20260821", 150.0, OptionRight.PUT)
    contract = to_ib_contract(spec)
    assert contract.secType == "OPT"
    assert contract.strike == 150.0
    assert contract.right == "P"
    assert contract.multiplier == "100"


def test_to_ib_contract_builds_future():
    contract = to_ib_contract(future_spec(BFF_SYMBOL, "20260731", BFF_EXCHANGE))
    assert contract.secType == "FUT"
    assert contract.symbol == BFF_SYMBOL
    assert contract.exchange == BFF_EXCHANGE


def test_to_ib_contract_rejects_incomplete_option():
    spec = InstrumentSpec(instrument=InstrumentType.OPTION, symbol="AMD")
    with pytest.raises(ContractResolutionError):
        to_ib_contract(spec)


def test_to_ib_contract_rejects_future_without_expiry():
    spec = InstrumentSpec(instrument=InstrumentType.FUTURE, symbol=BFF_SYMBOL)
    with pytest.raises(ContractResolutionError):
        to_ib_contract(spec)


def test_spec_roundtrips_through_dict():
    spec = option_spec("AMD", "20260821", 150.0, OptionRight.CALL)
    restored = InstrumentSpec.from_dict(spec.to_dict())
    assert restored == spec


def test_spec_from_dict_ignores_unknown_keys():
    payload = option_spec("AMD", "20260821", 150.0, OptionRight.CALL).to_dict()
    payload["legacy_field"] = "ignored"
    assert InstrumentSpec.from_dict(payload).symbol == "AMD"


def test_select_expiry_respects_event_date_and_min_days():
    available = ["20260731", "20260807", "20260821", "20260918"]
    chosen = select_expiry(
        available=available,
        not_before=date(2026, 8, 5),
        min_days=14,
        reference=date(2026, 7, 27),
    )
    assert chosen == "20260821"


def test_select_expiry_returns_none_when_all_too_close():
    chosen = select_expiry(
        available=["20260728", "20260729"],
        not_before=date(2026, 7, 27),
        min_days=14,
        reference=date(2026, 7, 27),
    )
    assert chosen is None


def test_select_expiry_uses_min_days_floor_when_event_is_near():
    chosen = select_expiry(
        available=["20260801", "20260815", "20260901"],
        not_before=date(2026, 7, 28),
        min_days=14,
        reference=date(2026, 7, 27),
    )
    assert chosen == "20260815"


def test_days_to_expiry():
    spec = option_spec("AMD", "20260821", 150.0, OptionRight.CALL)
    assert days_to_expiry(spec, reference=date(2026, 7, 27)) == 25


def test_days_to_expiry_none_for_stock():
    assert days_to_expiry(stock_spec("AMD")) is None


def test_next_bff_expiry_lands_on_friday():
    expiry = next_bff_expiry(reference=date(2026, 7, 27), min_days=2)
    assert expiry.weekday() == 4
    assert expiry == date(2026, 7, 31)


def test_next_bff_expiry_skips_to_following_friday_when_too_close():
    expiry = next_bff_expiry(reference=date(2026, 7, 30), min_days=2)
    assert expiry.weekday() == 4
    assert expiry == date(2026, 8, 7)
