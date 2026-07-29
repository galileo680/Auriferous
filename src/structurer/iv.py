from __future__ import annotations

import asyncio
import math

import structlog

from src.broker.contracts import stock_spec, to_ib_contract
from src.structurer.models import IVProfile

IV_HISTORY_DURATION = "1 Y"
IV_BAR_SIZE = "1 day"
IV_WHAT_TO_SHOW = "OPTION_IMPLIED_VOLATILITY"
MIN_IV_SAMPLES = 60
REALIZED_WINDOW = 20
TRADING_DAYS = 252


def percentile_rank(series: list[float], value: float) -> float | None:
    clean = [v for v in series if v is not None and math.isfinite(v) and v > 0]
    if len(clean) < MIN_IV_SAMPLES or not math.isfinite(value):
        return None

    below = sum(1 for v in clean if v < value)
    equal = sum(1 for v in clean if v == value)
    return (below + 0.5 * equal) / len(clean) * 100


def realized_volatility(closes: list[float], window: int = REALIZED_WINDOW) -> float | None:
    clean = [c for c in closes if c is not None and math.isfinite(c) and c > 0]
    if len(clean) < window + 1:
        return None

    tail = clean[-(window + 1):]
    returns = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    if len(returns) < 2:
        return None

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)


class IVAnalyzer:

    def __init__(self, broker) -> None:
        self._broker = broker
        self._logger = structlog.get_logger("IVAnalyzer")

    async def profile(self, ticker: str) -> IVProfile:
        history = await self._fetch_iv_history(ticker)
        realized = await self._fetch_realized_vol(ticker)

        if not history:
            return IVProfile(
                realized_vol=realized,
                source="iv history unavailable — structurer will prefer spreads",
            )

        current = history[-1]
        rank = percentile_rank(history, current)

        profile = IVProfile(
            iv_current=current,
            iv_rank=rank,
            realized_vol=realized,
            sample_size=len(history),
            source="ibkr OPTION_IMPLIED_VOLATILITY",
        )

        if rank is None:
            profile.source = f"only {len(history)} samples — need {MIN_IV_SAMPLES} for a rank"

        self._logger.info(
            "iv_profile",
            ticker=ticker,
            iv_current=round(current, 4) if current else None,
            iv_rank=round(rank, 1) if rank is not None else None,
            realized_vol=round(realized, 4) if realized else None,
            samples=len(history),
        )
        return profile

    async def _fetch_iv_history(self, ticker: str) -> list[float]:
        try:
            contract = to_ib_contract(stock_spec(ticker))
            qualified = await self._broker.ib.qualifyContractsAsync(contract)
            if not qualified:
                return []

            bars = await self._broker.ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime="",
                durationStr=IV_HISTORY_DURATION,
                barSizeSetting=IV_BAR_SIZE,
                whatToShow=IV_WHAT_TO_SHOW,
                useRTH=True,
                formatDate=1,
            )
            return [
                float(bar.close)
                for bar in (bars or [])
                if bar.close is not None and math.isfinite(float(bar.close)) and bar.close > 0
            ]
        except Exception as e:
            self._logger.warning("iv_history_failed", ticker=ticker, error=str(e))
            return []

    async def _fetch_realized_vol(self, ticker: str) -> float | None:
        def fetch() -> list[float]:
            import yfinance as yf

            frame = yf.Ticker(ticker).history(period="3mo", interval="1d")
            if frame is None or frame.empty:
                return []
            return [float(c) for c in frame["Close"].dropna().tolist()]

        try:
            loop = asyncio.get_event_loop()
            closes = await loop.run_in_executor(None, fetch)
            return realized_volatility(closes)
        except Exception as e:
            self._logger.debug("realized_vol_failed", ticker=ticker, error=str(e))
            return None
