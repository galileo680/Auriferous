from __future__ import annotations

import math
from datetime import date, timedelta

import structlog

from src.broker.contracts import option_spec, stock_spec
from src.broker.futures import FutureSelector
from src.broker.interface import BrokerInterface
from src.broker.models import InstrumentSpec, OptionRight, OrderSide
from src.broker.options import OptionSelector, check_liquidity, rank_by_delta, strikes_near
from src.core.config import StructurerConfig
from src.sentinel.models import MARKET_CRYPTO
from src.structurer.models import (
    InstrumentChoice,
    InstrumentDecision,
    IVProfile,
    StructuredTrade,
    StructureOutcome,
    StructureResult,
    TradeLeg,
)

SPREAD_STRIKE_SCAN = 10


class TradeBuilder:

    def __init__(
        self,
        broker: BrokerInterface,
        config: StructurerConfig,
        equity: float,
    ) -> None:
        self._broker = broker
        self._config = config
        self._equity = equity
        self._options = OptionSelector(broker, config)
        self._futures = FutureSelector(broker)
        self._logger = structlog.get_logger("TradeBuilder")

    async def build(
        self,
        ticker: str,
        market: str,
        direction: str,
        decision: InstrumentDecision,
        conviction: float,
        horizon_days: int,
        target_move_pct: float,
        iv: IVProfile,
        event_date: date | None,
        invalidation: list[str],
    ) -> StructureResult:
        if decision.choice is InstrumentChoice.SKIP:
            return StructureResult(
                outcome=StructureOutcome.SKIP_HIGH_IV,
                decision=decision,
                reason=decision.reason,
            )

        common = dict(
            ticker=ticker,
            market=market,
            direction=direction,
            conviction=conviction,
            horizon_days=horizon_days,
            target_move_pct=target_move_pct,
            iv=iv,
            invalidation=invalidation,
        )

        if decision.choice is InstrumentChoice.FUTURE:
            return await self._build_future(decision, **common)

        if decision.choice is InstrumentChoice.STOCK:
            return await self._build_stock(decision, **common)

        if decision.choice.is_spread:
            return await self._build_spread(decision, event_date, **common)

        return await self._build_long_option(decision, event_date, **common)

    async def _underlying_price(self, ticker: str) -> float | None:
        try:
            quote = await self._broker.get_quote(stock_spec(ticker))
        except Exception as e:
            self._logger.warning("underlying_quote_failed", ticker=ticker, error=str(e))
            return None
        return quote.last if quote.last > 0 else None

    async def _build_long_option(
        self,
        decision: InstrumentDecision,
        event_date: date | None,
        ticker: str,
        market: str,
        direction: str,
        conviction: float,
        horizon_days: int,
        target_move_pct: float,
        iv: IVProfile,
        invalidation: list[str],
    ) -> StructureResult:
        right = (
            OptionRight.CALL
            if decision.choice is InstrumentChoice.LONG_CALL
            else OptionRight.PUT
        )

        candidate = await self._options.find_contract(
            symbol=ticker,
            right=right,
            equity=self._equity,
            event_date=event_date,
        )
        if candidate is None:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_CONTRACT,
                decision=decision,
                reason="no option passed the expiry, delta and liquidity gates",
            )

        premium = candidate.premium
        trade = StructuredTrade(
            ticker=ticker,
            market=market,
            choice=decision.choice,
            direction=direction,
            legs=[TradeLeg(
                spec=candidate.spec,
                side=OrderSide.BUY,
                ratio=1,
                limit_price=round(candidate.quote.mid, 2),
            )],
            net_debit_per_unit=premium,
            max_loss_per_unit=premium,
            target_move_pct=target_move_pct,
            horizon_days=horizon_days,
            conviction=conviction,
            iv=iv,
            underlying_price=candidate.quote.underlying_price,
            invalidation=invalidation,
            notes=[
                f"delta {candidate.delta:.2f}" if candidate.delta else "delta unavailable",
                f"spread {candidate.liquidity.spread_pct:.1%}"
                if candidate.liquidity.spread_pct else "spread unknown",
            ],
        )
        return StructureResult(StructureOutcome.STRUCTURED, trade, decision, decision.reason)

    async def _build_spread(
        self,
        decision: InstrumentDecision,
        event_date: date | None,
        ticker: str,
        market: str,
        direction: str,
        conviction: float,
        horizon_days: int,
        target_move_pct: float,
        iv: IVProfile,
        invalidation: list[str],
    ) -> StructureResult:
        right = (
            OptionRight.CALL
            if decision.choice is InstrumentChoice.CALL_DEBIT_SPREAD
            else OptionRight.PUT
        )

        long_leg = await self._options.find_contract(
            symbol=ticker,
            right=right,
            equity=self._equity,
            event_date=event_date,
        )
        if long_leg is None:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_CONTRACT,
                decision=decision,
                reason="no long leg passed the gates",
            )

        short_leg = await self._find_short_leg(ticker, long_leg, right)
        if short_leg is None:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_CONTRACT,
                decision=decision,
                reason="no liquid short leg at the target delta — spread cannot be built",
            )

        net_debit = (long_leg.quote.mid - short_leg.quote.mid) * 100
        if net_debit <= 0:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_CONTRACT,
                decision=decision,
                reason="short leg is not cheaper than the long leg — quotes are unreliable",
            )

        premium_cap = self._equity * self._config.max_premium_pct_of_equity
        if net_debit > premium_cap:
            return StructureResult(
                outcome=StructureOutcome.SKIP_TOO_EXPENSIVE,
                decision=decision,
                reason=f"net debit ${net_debit:.0f} above cap ${premium_cap:.0f}",
            )

        trade = StructuredTrade(
            ticker=ticker,
            market=market,
            choice=decision.choice,
            direction=direction,
            legs=[
                TradeLeg(long_leg.spec, OrderSide.BUY, 1, round(long_leg.quote.mid, 2)),
                TradeLeg(short_leg.spec, OrderSide.SELL, 1, round(short_leg.quote.mid, 2)),
            ],
            net_debit_per_unit=net_debit,
            max_loss_per_unit=net_debit,
            target_move_pct=target_move_pct,
            horizon_days=horizon_days,
            conviction=conviction,
            iv=iv,
            underlying_price=long_leg.quote.underlying_price,
            invalidation=invalidation,
            notes=[
                f"long {long_leg.spec.strike} / short {short_leg.spec.strike}",
                f"max profit ${abs(short_leg.spec.strike - long_leg.spec.strike) * 100 - net_debit:.0f}",
            ],
        )
        return StructureResult(StructureOutcome.STRUCTURED, trade, decision, decision.reason)

    async def _find_short_leg(self, ticker: str, long_leg, right: OptionRight):
        expiry = long_leg.spec.expiry
        strikes = await self._broker.get_option_strikes(ticker, expiry)

        if right is OptionRight.CALL:
            candidates = [s for s in strikes if s > long_leg.spec.strike]
        else:
            candidates = [s for s in strikes if s < long_leg.spec.strike]

        if not candidates:
            return None

        shortlist = strikes_near(candidates, long_leg.spec.strike, SPREAD_STRIKE_SCAN)
        quotes = []
        for strike in shortlist:
            try:
                quote = await self._broker.get_option_quote(
                    option_spec(ticker, expiry, strike, right)
                )
            except Exception:
                continue
            if quote.has_greeks() and quote.bid > 0:
                quotes.append(quote)

        if not quotes:
            return None

        target = self._config.spread_short_delta
        ranked = rank_by_delta(quotes, target, target)

        for quote in ranked:
            liquidity = check_liquidity(quote, self._config, self._equity)
            if quote.bid > 0 and quote.ask > 0 and liquidity.spread_pct is not None:
                if liquidity.spread_pct <= self._config.max_spread_pct * 1.5:
                    return _AsCandidate(quote)
        return None

    async def _build_stock(
        self,
        decision: InstrumentDecision,
        ticker: str,
        market: str,
        direction: str,
        conviction: float,
        horizon_days: int,
        target_move_pct: float,
        iv: IVProfile,
        invalidation: list[str],
    ) -> StructureResult:
        price = await self._underlying_price(ticker)
        if price is None:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_PRICE,
                decision=decision,
                reason="no usable quote for the underlying",
            )

        if price > self._config.max_stock_price:
            return StructureResult(
                outcome=StructureOutcome.SKIP_TOO_EXPENSIVE,
                decision=decision,
                reason=f"share price ${price:.2f} above the ${self._config.max_stock_price:.0f} ceiling",
            )

        stop_distance = price * 0.15
        trade = StructuredTrade(
            ticker=ticker,
            market=market,
            choice=InstrumentChoice.STOCK,
            direction=direction,
            legs=[TradeLeg(stock_spec(ticker), OrderSide.BUY, 1, round(price, 2))],
            net_debit_per_unit=price,
            max_loss_per_unit=stop_distance,
            target_move_pct=target_move_pct,
            horizon_days=horizon_days,
            conviction=conviction,
            iv=iv,
            underlying_price=price,
            invalidation=invalidation,
            notes=["stop distance set by the risk governor at execution time"],
        )
        return StructureResult(StructureOutcome.STRUCTURED, trade, decision, decision.reason)

    async def _build_future(
        self,
        decision: InstrumentDecision,
        ticker: str,
        market: str,
        direction: str,
        conviction: float,
        horizon_days: int,
        target_move_pct: float,
        iv: IVProfile,
        invalidation: list[str],
    ) -> StructureResult:
        from src.broker.contracts import BFF_EXCHANGE, BFF_SYMBOL, future_spec

        try:
            expirations = await self._broker.get_future_expirations(BFF_SYMBOL, BFF_EXCHANGE)
            if not expirations:
                raise ValueError("no BFF contracts listed")
            probe = await self._broker.qualify(
                future_spec(BFF_SYMBOL, expirations[0], BFF_EXCHANGE)
            )
            quote = await self._broker.get_quote(probe)
        except Exception as e:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_PRICE,
                decision=decision,
                reason=f"BFF reference price unavailable: {e}",
            )

        if quote.last <= 0:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_PRICE,
                decision=decision,
                reason="BFF quote is empty — CME data subscription may be missing",
            )

        candidate = await self._futures.find_bff(
            horizon_days=horizon_days,
            available_capital=self._equity,
            reference_price=quote.last,
        )
        if candidate is None:
            return StructureResult(
                outcome=StructureOutcome.SKIP_NO_CONTRACT,
                decision=decision,
                reason="no BFF contract fits the horizon and the futures bucket",
            )

        side = OrderSide.BUY if direction.upper() == "LONG" else OrderSide.SELL
        trade = StructuredTrade(
            ticker=ticker,
            market=MARKET_CRYPTO,
            choice=InstrumentChoice.FUTURE,
            direction=direction,
            legs=[TradeLeg(candidate.spec, side, 1, round(quote.last, 2))],
            net_debit_per_unit=candidate.initial_margin,
            max_loss_per_unit=candidate.initial_margin,
            target_move_pct=target_move_pct,
            horizon_days=horizon_days,
            conviction=conviction,
            iv=iv,
            underlying_price=quote.last,
            invalidation=invalidation,
            notes=[
                f"initial margin ${candidate.initial_margin:.0f}",
                f"{candidate.days_to_expiry}d to expiry",
                "ATR stop is placed with the broker at execution",
            ],
        )
        return StructureResult(StructureOutcome.STRUCTURED, trade, decision, decision.reason)


class _AsCandidate:

    def __init__(self, quote) -> None:
        self.quote = quote
        self.spec = quote.spec
        self.delta = quote.delta
        self.premium = quote.premium_per_contract
