from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import StructurerConfig
from src.sentinel.models import MARKET_CRYPTO, MARKET_EQUITY
from src.structurer.decision import choose_instrument, is_binary_event
from src.structurer.iv import percentile_rank, realized_volatility
from src.structurer.models import (
    InstrumentChoice,
    IVProfile,
    StructureOutcome,
    StructureResult,
)

CONFIG = StructurerConfig()


def pick(
    direction: str = "LONG",
    conviction: float = 0.75,
    horizon: int = 10,
    catalyst: str = "FDA_DECISION",
    iv_rank: float | None = 40.0,
    price: float | None = 25.0,
    market: str = MARKET_EQUITY,
):
    return choose_instrument(
        market=market,
        direction=direction,
        conviction=conviction,
        horizon_days=horizon,
        catalyst_type=catalyst,
        iv_rank=iv_rank,
        underlying_price=price,
        config=CONFIG,
    )


def test_binary_event_with_low_iv_and_conviction_buys_raw_premium():
    decision = pick()
    assert decision.choice is InstrumentChoice.LONG_CALL
    assert decision.binary_event
    assert "convexity" in decision.reason


def test_binary_short_thesis_buys_puts():
    assert pick(direction="SHORT").choice is InstrumentChoice.LONG_PUT


def test_elevated_iv_downgrades_to_a_spread():
    decision = pick(iv_rank=70.0)
    assert decision.choice is InstrumentChoice.CALL_DEBIT_SPREAD
    assert "elevated" in decision.reason


def test_extreme_iv_skips_the_trade_entirely():
    decision = pick(iv_rank=85.0)
    assert decision.choice is InstrumentChoice.SKIP
    assert "volatility crush" in decision.reason


def test_unknown_iv_degrades_safely_to_a_spread():
    decision = pick(iv_rank=None)
    assert decision.choice is InstrumentChoice.CALL_DEBIT_SPREAD
    assert "IV rank unknown" in decision.reason


def test_unknown_iv_on_a_short_thesis_gives_a_put_spread():
    assert pick(direction="SHORT", iv_rank=None).choice is InstrumentChoice.PUT_DEBIT_SPREAD


def test_low_conviction_binary_event_caps_cost_with_a_spread():
    decision = pick(conviction=0.45)
    assert decision.choice is InstrumentChoice.CALL_DEBIT_SPREAD
    assert "below" in decision.reason


def test_cheap_stock_with_a_long_horizon_is_bought_outright():
    decision = pick(catalyst="GUIDANCE", horizon=45, price=32.0)
    assert decision.choice is InstrumentChoice.STOCK
    assert not decision.binary_event


def test_expensive_stock_with_a_long_horizon_uses_a_call():
    assert pick(catalyst="GUIDANCE", horizon=45, price=140.0).choice is InstrumentChoice.LONG_CALL


def test_short_thesis_never_borrows_stock():
    decision = pick(direction="SHORT", catalyst="GUIDANCE", horizon=45, price=20.0)
    assert decision.choice is InstrumentChoice.LONG_PUT
    assert "never by borrowing stock" in decision.reason


def test_crypto_always_routes_to_futures():
    decision = pick(market=MARKET_CRYPTO, catalyst="CRYPTO_FLOW")
    assert decision.choice is InstrumentChoice.FUTURE


def test_crypto_ignores_the_iv_skip_gate():
    assert pick(market=MARKET_CRYPTO, iv_rank=95.0).choice is InstrumentChoice.FUTURE


def test_long_horizon_disqualifies_a_binary_classification():
    assert not is_binary_event("FDA_DECISION", 60, CONFIG)
    assert is_binary_event("FDA_DECISION", 14, CONFIG)


def test_non_binary_catalyst_types_are_not_binary():
    assert not is_binary_event("GUIDANCE", 5, CONFIG)
    assert not is_binary_event(None, 5, CONFIG)


def test_instrument_choice_classification_helpers():
    assert InstrumentChoice.LONG_CALL.is_option
    assert InstrumentChoice.LONG_CALL.is_long_premium
    assert not InstrumentChoice.LONG_CALL.is_spread
    assert InstrumentChoice.CALL_DEBIT_SPREAD.is_spread
    assert InstrumentChoice.CALL_DEBIT_SPREAD.is_option
    assert not InstrumentChoice.CALL_DEBIT_SPREAD.is_long_premium
    assert not InstrumentChoice.STOCK.is_option


def test_percentile_rank_positions_current_iv():
    series = [float(i) for i in range(1, 101)]
    assert percentile_rank(series, 50.0) == pytest.approx(49.5)
    assert percentile_rank(series, 1.0) == pytest.approx(0.5)
    assert percentile_rank(series, 100.0) == pytest.approx(99.5)


def test_percentile_rank_needs_enough_history():
    assert percentile_rank([0.3, 0.4, 0.5], 0.4) is None


def test_percentile_rank_ignores_invalid_samples():
    series = [0.0, -1.0] + [0.3] * 80
    assert percentile_rank(series, 0.3) is not None


def test_realized_volatility_is_zero_for_a_flat_series():
    assert realized_volatility([100.0] * 25) == pytest.approx(0.0)


def test_realized_volatility_grows_with_dispersion():
    calm = [100.0 + (i % 2) * 0.1 for i in range(30)]
    wild = [100.0 + (i % 2) * 8.0 for i in range(30)]
    assert realized_volatility(wild) > realized_volatility(calm)


def test_realized_volatility_needs_enough_history():
    assert realized_volatility([100.0, 101.0]) is None


def test_iv_profile_ratio_against_realized():
    profile = IVProfile(iv_current=0.60, realized_vol=0.30)
    assert profile.iv_vs_realized == pytest.approx(2.0)


def test_iv_profile_ratio_is_none_without_data():
    assert IVProfile(iv_current=0.60).iv_vs_realized is None
    assert IVProfile(realized_vol=0.30).iv_vs_realized is None


def test_structure_result_payload_records_the_skip_reason():
    result = StructureResult(
        outcome=StructureOutcome.SKIP_HIGH_IV,
        reason="IV rank 88 above 80",
    )
    payload = result.to_payload()
    assert payload["outcome"] == "SKIP_HIGH_IV"
    assert "88" in payload["reason"]
    assert not result.structured


def test_every_skip_outcome_is_flagged_as_a_skip():
    assert StructureOutcome.SKIP_NO_CONTRACT.is_skip
    assert StructureOutcome.SKIP_ERROR.is_skip
    assert not StructureOutcome.STRUCTURED.is_skip
