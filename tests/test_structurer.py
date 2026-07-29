from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import StructurerConfig
from src.sentinel.models import MARKET_CRYPTO, MARKET_EQUITY
from src.structurer.builder import TradeBuilder
from src.structurer.decision import choose_instrument, is_binary_event
from src.structurer.iv import percentile_rank, realized_volatility
from src.structurer.models import (
    InstrumentChoice,
    InstrumentDecision,
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


class _StubOptions:
    async def find_contract(self, **kwargs):
        return None


class _FakeBroker:
    def __init__(self, price: float) -> None:
        self._price = price

    async def get_quote(self, spec):
        return SimpleNamespace(last=self._price)


def build_with_illiquid_options(
    direction: str = "LONG",
    catalyst: str | None = "ACCOUNTING_RED_FLAG",
    price: float = 25.0,
    choice: InstrumentChoice = InstrumentChoice.LONG_CALL,
) -> StructureResult:
    async def runner():
        builder = TradeBuilder(_FakeBroker(price), CONFIG, equity=2450.0)
        builder._options = _StubOptions()
        return await builder.build(
            ticker="TEST",
            market=MARKET_EQUITY,
            direction=direction,
            decision=InstrumentDecision(choice=choice, reason="test", binary_event=True),
            conviction=0.70,
            horizon_days=10,
            target_move_pct=15.0,
            iv=IVProfile(),
            event_date=None,
            invalidation=[],
            catalyst_type=catalyst,
        )

    return asyncio.run(runner())


def test_niche_long_with_illiquid_options_falls_back_to_stock():
    result = build_with_illiquid_options()
    assert result.structured
    assert result.trade.choice is InstrumentChoice.STOCK
    assert any("stock fallback" in note for note in result.trade.notes)


def test_spread_choice_also_falls_back_for_niche_catalysts():
    result = build_with_illiquid_options(choice=InstrumentChoice.CALL_DEBIT_SPREAD)
    assert result.structured
    assert result.trade.choice is InstrumentChoice.STOCK


def test_short_thesis_never_uses_the_stock_fallback():
    result = build_with_illiquid_options(
        direction="SHORT", choice=InstrumentChoice.LONG_PUT
    )
    assert result.outcome is StructureOutcome.SKIP_NO_CONTRACT


def test_non_niche_catalyst_does_not_fall_back():
    result = build_with_illiquid_options(catalyst="GUIDANCE")
    assert result.outcome is StructureOutcome.SKIP_NO_CONTRACT


def test_fallback_respects_the_direct_stock_price_ceiling():
    result = build_with_illiquid_options(price=95.0)
    assert result.outcome is StructureOutcome.SKIP_NO_CONTRACT
