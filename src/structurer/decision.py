from __future__ import annotations

from typing import Optional

from src.core.config import StructurerConfig
from src.sentinel.models import MARKET_CRYPTO
from src.structurer.models import (
    BINARY_CATALYSTS,
    InstrumentChoice,
    InstrumentDecision,
)

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"


def is_binary_event(catalyst_type: str | None, horizon_days: int, config: StructurerConfig) -> bool:
    if not catalyst_type:
        return False
    return (
        catalyst_type.upper() in BINARY_CATALYSTS
        and horizon_days <= config.binary_event_max_horizon_days
    )


def choose_instrument(
    market: str,
    direction: str,
    conviction: float,
    horizon_days: int,
    catalyst_type: str | None,
    iv_rank: Optional[float],
    underlying_price: Optional[float],
    config: StructurerConfig,
) -> InstrumentDecision:
    if market == MARKET_CRYPTO:
        return InstrumentDecision(
            choice=InstrumentChoice.FUTURE,
            reason="crypto exposure is taken through CME futures",
        )

    binary = is_binary_event(catalyst_type, horizon_days, config)
    long_side = direction.upper() == DIRECTION_LONG

    if iv_rank is not None and iv_rank > config.iv_rank_skip_threshold:
        return InstrumentDecision(
            choice=InstrumentChoice.SKIP,
            reason=(
                f"IV rank {iv_rank:.0f} above {config.iv_rank_skip_threshold:.0f} — "
                f"premium would be eaten by the volatility crush"
            ),
            binary_event=binary,
        )

    if binary:
        if iv_rank is None:
            return InstrumentDecision(
                choice=(
                    InstrumentChoice.CALL_DEBIT_SPREAD if long_side
                    else InstrumentChoice.PUT_DEBIT_SPREAD
                ),
                reason="IV rank unknown — spread is the safe structure when volatility is unpriced",
                binary_event=True,
            )

        if iv_rank >= config.iv_rank_spread_threshold:
            return InstrumentDecision(
                choice=(
                    InstrumentChoice.CALL_DEBIT_SPREAD if long_side
                    else InstrumentChoice.PUT_DEBIT_SPREAD
                ),
                reason=f"IV rank {iv_rank:.0f} is elevated — buying a spread instead of raw premium",
                binary_event=True,
            )

        if conviction >= config.min_conviction_for_long_premium:
            return InstrumentDecision(
                choice=InstrumentChoice.LONG_CALL if long_side else InstrumentChoice.LONG_PUT,
                reason=(
                    f"binary event in {horizon_days}d, conviction {conviction:.2f}, "
                    f"IV rank {iv_rank:.0f} — convexity is worth paying for"
                ),
                binary_event=True,
            )

        return InstrumentDecision(
            choice=(
                InstrumentChoice.CALL_DEBIT_SPREAD if long_side
                else InstrumentChoice.PUT_DEBIT_SPREAD
            ),
            reason=(
                f"binary event but conviction {conviction:.2f} below "
                f"{config.min_conviction_for_long_premium:.2f} — capping cost with a spread"
            ),
            binary_event=True,
        )

    if not long_side:
        return InstrumentDecision(
            choice=InstrumentChoice.LONG_PUT,
            reason="short thesis on a small cap — expressed with puts, never by borrowing stock",
            binary_event=False,
        )

    if (
        horizon_days > config.binary_event_max_horizon_days
        and underlying_price is not None
        and underlying_price <= config.stock_max_price_for_direct
    ):
        return InstrumentDecision(
            choice=InstrumentChoice.STOCK,
            reason=(
                f"directional drift over {horizon_days}d at ${underlying_price:.2f} — "
                f"stock avoids theta and the option spread"
            ),
            binary_event=False,
        )

    return InstrumentDecision(
        choice=InstrumentChoice.LONG_CALL,
        reason="directional long without a binary date — long call keeps risk defined",
        binary_event=False,
    )
