from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.contracts import option_spec
from src.broker.futures import needs_roll
from src.broker.models import OptionQuote, OptionRight
from src.broker.options import check_liquidity, rank_by_delta, strikes_near
from src.core.config import StructurerConfig

EQUITY = 2450.0


def make_quote(
    bid: float = 1.90,
    ask: float = 2.10,
    volume: int = 100,
    open_interest: int = 800,
    delta: float | None = 0.40,
    implied_vol: float | None = 0.55,
    strike: float = 150.0,
) -> OptionQuote:
    return OptionQuote(
        spec=option_spec("AMD", "20260821", strike, OptionRight.CALL),
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        volume=volume,
        open_interest=open_interest,
        implied_vol=implied_vol,
        delta=delta,
        gamma=0.01,
        theta=-0.05,
        vega=0.10,
        underlying_price=152.0,
        timestamp=datetime.utcnow(),
    )


def test_liquid_contract_passes():
    check = check_liquidity(make_quote(), StructurerConfig(), EQUITY)
    assert check.passed
    assert check.reason() == "ok"


def test_wide_spread_is_rejected():
    check = check_liquidity(make_quote(bid=1.60, ask=2.40), StructurerConfig(), EQUITY)
    assert not check.passed
    assert any("spread" in f for f in check.failures)


def test_spread_exactly_at_limit_passes():
    quote = make_quote(bid=1.88, ask=2.12)
    assert quote.spread_pct == pytest.approx(0.12)
    assert check_liquidity(quote, StructurerConfig(), EQUITY).passed


def test_low_open_interest_is_rejected():
    check = check_liquidity(make_quote(open_interest=100), StructurerConfig(), EQUITY)
    assert not check.passed
    assert any("open interest" in f for f in check.failures)


def test_low_volume_is_rejected():
    check = check_liquidity(make_quote(volume=5), StructurerConfig(), EQUITY)
    assert not check.passed
    assert any("volume" in f for f in check.failures)


def test_one_sided_market_is_rejected():
    check = check_liquidity(make_quote(bid=0.0), StructurerConfig(), EQUITY)
    assert not check.passed
    assert any("two-sided" in f for f in check.failures)


def test_premium_above_equity_cap_is_rejected():
    check = check_liquidity(make_quote(bid=3.00, ask=3.10), StructurerConfig(), EQUITY)
    assert not check.passed
    assert any("premium" in f for f in check.failures)
    assert check.premium == pytest.approx(305.0)


def test_premium_cap_scales_with_equity():
    quote = make_quote(bid=3.00, ask=3.10)
    assert check_liquidity(quote, StructurerConfig(), 10_000.0).passed


def test_multiple_failures_are_all_reported():
    check = check_liquidity(
        make_quote(bid=1.50, ask=2.50, volume=1, open_interest=10),
        StructurerConfig(),
        EQUITY,
    )
    assert not check.passed
    assert len(check.failures) >= 3


def test_rank_by_delta_prefers_target_midpoint():
    quotes = [
        make_quote(delta=0.70, strike=130.0),
        make_quote(delta=0.40, strike=150.0),
        make_quote(delta=0.15, strike=170.0),
    ]
    ranked = rank_by_delta(quotes, 0.35, 0.45)
    assert ranked[0].spec.strike == 150.0


def test_rank_by_delta_pushes_missing_greeks_last():
    quotes = [make_quote(delta=None, strike=130.0), make_quote(delta=0.42, strike=150.0)]
    assert rank_by_delta(quotes, 0.35, 0.45)[0].spec.strike == 150.0


def test_rank_by_delta_uses_absolute_delta_for_puts():
    quotes = [make_quote(delta=-0.40, strike=150.0), make_quote(delta=-0.80, strike=180.0)]
    assert rank_by_delta(quotes, 0.35, 0.45)[0].spec.strike == 150.0


def test_strikes_near_returns_sorted_window_around_price():
    strikes = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0]
    result = strikes_near(strikes, 135.0, 4)
    assert result == [120.0, 130.0, 140.0, 150.0]


def test_strikes_near_handles_empty_input():
    assert strikes_near([], 100.0, 4) == []


def test_premium_per_contract_uses_multiplier():
    assert make_quote(bid=1.90, ask=2.10).premium_per_contract == pytest.approx(200.0)


def test_needs_roll_true_on_roll_window():
    spec = option_spec("BFF", "20260731", 0, OptionRight.CALL)
    assert needs_roll(
        spec,
        roll_weekday=3,
        roll_hour_utc=20,
        now=date(2026, 7, 30),
        now_hour_utc=20,
        now_weekday=3,
    )


def test_needs_roll_false_before_roll_hour():
    spec = option_spec("BFF", "20260731", 0, OptionRight.CALL)
    assert not needs_roll(
        spec,
        roll_weekday=3,
        roll_hour_utc=20,
        now=date(2026, 7, 30),
        now_hour_utc=15,
        now_weekday=3,
    )


def test_needs_roll_true_when_already_expired():
    spec = option_spec("BFF", "20260724", 0, OptionRight.CALL)
    assert needs_roll(
        spec,
        roll_weekday=3,
        roll_hour_utc=20,
        now=date(2026, 7, 27),
        now_hour_utc=8,
        now_weekday=0,
    )
