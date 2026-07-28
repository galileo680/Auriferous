from __future__ import annotations

import asyncio

import structlog

from src.sentinel.universe import UniverseIndex
from src.triage.models import MarketContext

HISTORY_PERIOD = "1mo"
INTRADAY_PERIOD = "2d"
INTRADAY_INTERVAL = "5m"
VOLUME_WINDOW = 20


def compute_change_pct(current: float, reference: float) -> float | None:
    if reference <= 0:
        return None
    return (current - reference) / reference * 100


def compute_after_hours_move(
    regular_close: float,
    extended_last: float | None,
) -> float | None:
    if extended_last is None or regular_close <= 0:
        return None
    move = compute_change_pct(extended_last, regular_close)
    if move is None or abs(move) < 0.01:
        return None
    return move


class ContextBuilder:

    def __init__(self, universe: UniverseIndex) -> None:
        self._universe = universe
        self._logger = structlog.get_logger("ContextBuilder")

    async def build(self, ticker: str) -> MarketContext:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._build_sync, ticker)

    def _build_sync(self, ticker: str) -> MarketContext:
        entry = self._universe.get(ticker)
        context = MarketContext(
            ticker=ticker,
            market_cap=entry.market_cap if entry else None,
            sector=entry.sector if entry else None,
            has_options=bool(entry and entry.option_oi),
        )

        try:
            self._attach_daily(context)
        except Exception as e:
            context.warnings.append("daily history unavailable")
            self._logger.warning("context_daily_failed", ticker=ticker, error=str(e))

        try:
            self._attach_extended(context)
        except Exception as e:
            context.warnings.append("extended hours data unavailable")
            self._logger.debug("context_extended_failed", ticker=ticker, error=str(e))

        return context

    def _attach_daily(self, context: MarketContext) -> None:
        import yfinance as yf

        frame = yf.Ticker(context.ticker).history(period=HISTORY_PERIOD, interval="1d")
        if frame is None or frame.empty:
            context.warnings.append("no daily bars returned")
            return

        frame = frame.dropna(subset=["Close", "Volume"])
        if len(frame) < 2:
            context.warnings.append("insufficient daily history")
            return

        last = frame.iloc[-1]
        context.last_price = float(last["Close"])
        context.change_1d_pct = compute_change_pct(
            context.last_price, float(frame.iloc[-2]["Close"])
        )

        if len(frame) >= 6:
            context.change_5d_pct = compute_change_pct(
                context.last_price, float(frame.iloc[-6]["Close"])
            )

        history = frame.iloc[:-1].tail(VOLUME_WINDOW)
        if len(history) >= 5:
            average = float(history["Volume"].mean())
            if average > 0:
                context.volume_ratio = float(last["Volume"]) / average

    def _attach_extended(self, context: MarketContext) -> None:
        import yfinance as yf

        if context.last_price is None:
            return

        frame = yf.Ticker(context.ticker).history(
            period=INTRADAY_PERIOD,
            interval=INTRADAY_INTERVAL,
            prepost=True,
        )
        if frame is None or frame.empty:
            return

        frame = frame.dropna(subset=["Close"])
        if frame.empty:
            return

        extended_last = float(frame["Close"].iloc[-1])
        context.after_hours_move_pct = compute_after_hours_move(
            context.last_price, extended_last
        )
