from __future__ import annotations

from typing import Optional

DELTA_ASSUMED = 0.40
FUTURE_STOP_FRACTION = 0.10
DEFAULT_ODDS = 1.0

BUCKET_LOW = "LOW"
BUCKET_MEDIUM = "MEDIUM"
BUCKET_HIGH = "HIGH"

LONG_PREMIUM_CHOICES = ("LONG_CALL", "LONG_PUT")
SPREAD_CHOICES = ("CALL_DEBIT_SPREAD", "PUT_DEBIT_SPREAD")


def conviction_bucket(conviction: float) -> str:
    if conviction < 0.65:
        return BUCKET_LOW
    if conviction < 0.75:
        return BUCKET_MEDIUM
    return BUCKET_HIGH


def kelly_fraction(p: float, b: float) -> float:
    if b <= 0:
        return 0.0
    return (p * b - (1.0 - p)) / b


def position_fraction(p: float, b: float, kelly_multiplier: float, cap: float) -> float:
    raw = kelly_multiplier * kelly_fraction(p, b)
    return min(max(raw, 0.0), cap)


def payoff_odds(
    choice: str,
    target_move_pct: float,
    net_debit: float,
    max_loss: float,
    underlying_price: Optional[float],
    strikes: list[float],
) -> float:
    move = abs(target_move_pct) / 100.0

    if choice in LONG_PREMIUM_CHOICES:
        if underlying_price and net_debit > 0:
            leverage = DELTA_ASSUMED * underlying_price * 100.0 / net_debit
            return move * leverage
        return DEFAULT_ODDS

    if choice in SPREAD_CHOICES:
        if len(strikes) >= 2 and net_debit > 0:
            width = abs(strikes[0] - strikes[1]) * 100.0
            max_profit = width - net_debit
            if max_profit > 0:
                return max_profit / net_debit
        return DEFAULT_ODDS

    if choice == "STOCK":
        if net_debit > 0 and max_loss > 0:
            stop_fraction = max_loss / net_debit
            if stop_fraction > 0:
                return move / stop_fraction
        return DEFAULT_ODDS

    if choice == "FUTURE":
        return move / FUTURE_STOP_FRACTION

    return DEFAULT_ODDS
