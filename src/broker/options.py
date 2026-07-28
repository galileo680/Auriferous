from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog

from src.broker.contracts import option_spec, parse_expiry, select_expiry
from src.broker.interface import BrokerInterface
from src.broker.models import (
    InstrumentSpec,
    LiquidityCheck,
    OptionQuote,
    OptionRight,
)
from src.core.config import StructurerConfig

STRIKE_SCAN_WIDTH = 6
THRESHOLD_EPSILON = 1e-9


def check_liquidity(
    quote: OptionQuote,
    config: StructurerConfig,
    equity: float,
) -> LiquidityCheck:
    failures: list[str] = []

    if quote.bid <= 0 or quote.ask <= 0:
        failures.append("no two-sided market")

    spread = quote.spread_pct
    if spread is None:
        failures.append("spread not computable")
    elif spread > config.max_spread_pct + THRESHOLD_EPSILON:
        failures.append(f"spread {spread:.1%} > {config.max_spread_pct:.0%}")

    if quote.open_interest < config.min_open_interest:
        failures.append(
            f"open interest {quote.open_interest} < {config.min_open_interest}"
        )

    if quote.volume < config.min_volume:
        failures.append(f"volume {quote.volume} < {config.min_volume}")

    premium = quote.premium_per_contract
    premium_cap = equity * config.max_premium_pct_of_equity
    if premium > premium_cap + THRESHOLD_EPSILON:
        failures.append(f"premium ${premium:.0f} > cap ${premium_cap:.0f}")

    return LiquidityCheck(
        passed=not failures,
        failures=failures,
        spread_pct=spread,
        open_interest=quote.open_interest,
        volume=quote.volume,
        premium=premium,
    )


def rank_by_delta(
    quotes: list[OptionQuote],
    target_min: float,
    target_max: float,
) -> list[OptionQuote]:
    target_mid = (target_min + target_max) / 2

    def distance(quote: OptionQuote) -> float:
        if quote.delta is None:
            return float("inf")
        return abs(abs(quote.delta) - target_mid)

    return sorted(quotes, key=distance)


def strikes_near(strikes: list[float], reference: float, width: int) -> list[float]:
    if not strikes:
        return []
    ordered = sorted(strikes, key=lambda s: abs(s - reference))
    return sorted(ordered[:width])


@dataclass
class OptionCandidate:
    spec: InstrumentSpec
    quote: OptionQuote
    liquidity: LiquidityCheck

    @property
    def premium(self) -> float:
        return self.quote.premium_per_contract

    @property
    def delta(self) -> float | None:
        return self.quote.delta


class OptionSelector:

    def __init__(self, broker: BrokerInterface, config: StructurerConfig) -> None:
        self._broker = broker
        self._config = config
        self._logger = structlog.get_logger("OptionSelector")

    async def find_contract(
        self,
        symbol: str,
        right: OptionRight,
        equity: float,
        event_date: date | None = None,
        reference: date | None = None,
    ) -> OptionCandidate | None:
        expirations = await self._broker.get_option_expirations(symbol)
        if not expirations:
            self._logger.info("option_no_expirations", ticker=symbol)
            return None

        not_before = event_date or (reference or date.today())
        expiry = select_expiry(
            available=expirations,
            not_before=not_before,
            min_days=max(
                self._config.min_days_to_expiry,
                self._config.event_expiry_buffer_days,
            ),
            reference=reference,
        )
        if expiry is None:
            self._logger.info(
                "option_no_suitable_expiry",
                ticker=symbol,
                event_date=str(not_before),
                min_dte=self._config.min_days_to_expiry,
            )
            return None

        underlying = await self._broker.get_quote(
            InstrumentSpec(instrument=self._stock_type(), symbol=symbol)
        )
        if underlying.last <= 0:
            self._logger.info("option_no_underlying_price", ticker=symbol)
            return None

        strikes = await self._broker.get_option_strikes(symbol, expiry)
        shortlist = strikes_near(strikes, underlying.last, STRIKE_SCAN_WIDTH)
        if not shortlist:
            self._logger.info("option_no_strikes", ticker=symbol, expiry=expiry)
            return None

        quotes: list[OptionQuote] = []
        for strike in shortlist:
            spec = option_spec(symbol, expiry, strike, right)
            try:
                quotes.append(await self._broker.get_option_quote(spec))
            except Exception as e:
                self._logger.debug(
                    "option_quote_failed",
                    ticker=symbol,
                    strike=strike,
                    error=str(e),
                )

        with_greeks = [q for q in quotes if q.has_greeks()]
        if not with_greeks:
            self._logger.warning(
                "option_no_greeks",
                ticker=symbol,
                expiry=expiry,
                note="OPRA subscription required for delta-based selection",
            )
            return None

        rejected: list[str] = []
        for quote in rank_by_delta(
            with_greeks,
            self._config.target_delta_min,
            self._config.target_delta_max,
        ):
            liquidity = check_liquidity(quote, self._config, equity)
            if liquidity.passed:
                self._logger.info(
                    "option_selected",
                    ticker=symbol,
                    contract=quote.spec.describe(),
                    delta=round(quote.delta, 3) if quote.delta else None,
                    premium=round(quote.premium_per_contract, 2),
                    spread_pct=round(liquidity.spread_pct, 4) if liquidity.spread_pct else None,
                    dte=(parse_expiry(expiry) - (reference or date.today())).days,
                )
                return OptionCandidate(spec=quote.spec, quote=quote, liquidity=liquidity)
            rejected.append(f"{quote.spec.strike}{quote.spec.right}: {liquidity.reason()}")

        self._logger.info(
            "option_all_rejected",
            ticker=symbol,
            expiry=expiry,
            rejections=rejected[:5],
        )
        return None

    @staticmethod
    def _stock_type():
        from src.broker.models import InstrumentType

        return InstrumentType.STOCK
