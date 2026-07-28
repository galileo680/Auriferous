from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import httpx
import structlog

from src.core.clock import utcnow_naive
from src.sentinel.models import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    MARKET_CRYPTO,
    PRIORITY_NORMAL,
    SOURCE_CRYPTO_FUNDING,
    SOURCE_CRYPTO_OI,
    EventSource,
    RawEvent,
)

PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
OPEN_INTEREST_URL = "https://fapi.binance.com/futures/data/openInterestHist"
REQUEST_TIMEOUT = 15.0

TRACKED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
CME_TRADABLE = {"BTCUSDT": "BFF", "ETHUSDT": "MET"}

FUNDING_THRESHOLD = 0.0005
FUNDING_CONSECUTIVE = 3
OI_DIVERGENCE_THRESHOLD = 0.10
OI_PRICE_FLAT_THRESHOLD = 0.01


@dataclass
class FundingReading:
    symbol: str
    funding_rate: float
    mark_price: float


def funding_is_extreme(history: list[float], threshold: float, required: int) -> bool:
    if len(history) < required:
        return False
    recent = history[-required:]
    if all(rate > threshold for rate in recent):
        return True
    return all(rate < -threshold for rate in recent)


def funding_direction(funding_rate: float) -> str:
    return DIRECTION_SHORT if funding_rate > 0 else DIRECTION_LONG


def oi_divergence(oi_change_pct: float, price_change_pct: float) -> str | None:
    if abs(price_change_pct) > OI_PRICE_FLAT_THRESHOLD:
        return None
    if oi_change_pct >= OI_DIVERGENCE_THRESHOLD:
        return "POSITION_BUILDING"
    if oi_change_pct <= -OI_DIVERGENCE_THRESHOLD:
        return "POSITION_UNWINDING"
    return None


class CryptoFlowSource(EventSource):

    name = "crypto_flow"
    market = MARKET_CRYPTO

    def __init__(self, symbols: tuple[str, ...] = TRACKED_SYMBOLS) -> None:
        self._symbols = symbols
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        self._logger = structlog.get_logger("CryptoFlowSource")
        self._funding_history: dict[str, deque[float]] = {
            symbol: deque(maxlen=FUNDING_CONSECUTIVE * 2) for symbol in symbols
        }
        self._seen: set[str] = set()

    async def close(self) -> None:
        await self._client.aclose()

    async def poll(self) -> list[RawEvent]:
        events: list[RawEvent] = []
        events.extend(await self._poll_funding())
        events.extend(await self._poll_open_interest())
        return events

    async def _poll_funding(self) -> list[RawEvent]:
        try:
            response = await self._client.get(PREMIUM_INDEX_URL)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            self._logger.warning("crypto_funding_unavailable", error=str(e))
            return []

        readings = {
            item["symbol"]: FundingReading(
                symbol=item["symbol"],
                funding_rate=float(item.get("lastFundingRate", 0.0)),
                mark_price=float(item.get("markPrice", 0.0)),
            )
            for item in payload
            if item.get("symbol") in self._symbols
        }

        stamp = utcnow_naive().strftime("%Y%m%d%H")
        events: list[RawEvent] = []

        for symbol, reading in readings.items():
            history = self._funding_history[symbol]
            history.append(reading.funding_rate)

            if not funding_is_extreme(list(history), FUNDING_THRESHOLD, FUNDING_CONSECUTIVE):
                continue

            dedup_key = f"funding:{symbol}:{stamp}"
            if dedup_key in self._seen:
                continue
            self._seen.add(dedup_key)

            events.append(RawEvent(
                source=SOURCE_CRYPTO_FUNDING,
                ticker=CME_TRADABLE.get(symbol, symbol),
                market=MARKET_CRYPTO,
                dedup_key=dedup_key,
                priority=PRIORITY_NORMAL,
                direction=funding_direction(reading.funding_rate),
                payload={
                    "reference_symbol": symbol,
                    "funding_rate": reading.funding_rate,
                    "funding_rate_pct": round(reading.funding_rate * 100, 4),
                    "mark_price": reading.mark_price,
                    "consecutive_periods": FUNDING_CONSECUTIVE,
                    "note": "crowded positioning — contrarian setup, traded via CME",
                },
            ))

        if events:
            self._logger.info("crypto_funding_events", count=len(events))
        return events

    async def _poll_open_interest(self) -> list[RawEvent]:
        events: list[RawEvent] = []
        stamp = utcnow_naive().strftime("%Y%m%d%H")

        for symbol in self._symbols:
            try:
                response = await self._client.get(
                    OPEN_INTEREST_URL,
                    params={"symbol": symbol, "period": "1h", "limit": 24},
                )
                response.raise_for_status()
                history = response.json()
            except (httpx.HTTPError, ValueError) as e:
                self._logger.warning("crypto_oi_unavailable", symbol=symbol, error=str(e))
                continue

            if len(history) < 24:
                continue

            first, last = history[0], history[-1]
            oi_start = float(first.get("sumOpenInterest", 0.0))
            oi_end = float(last.get("sumOpenInterest", 0.0))
            value_start = float(first.get("sumOpenInterestValue", 0.0))
            value_end = float(last.get("sumOpenInterestValue", 0.0))

            if oi_start <= 0 or value_start <= 0 or oi_end <= 0:
                continue

            oi_change = (oi_end - oi_start) / oi_start
            price_start = value_start / oi_start
            price_end = value_end / oi_end
            price_change = (price_end - price_start) / price_start if price_start else 0.0

            pattern = oi_divergence(oi_change, price_change)
            if pattern is None:
                continue

            dedup_key = f"oi:{symbol}:{stamp}"
            if dedup_key in self._seen:
                continue
            self._seen.add(dedup_key)

            events.append(RawEvent(
                source=SOURCE_CRYPTO_OI,
                ticker=CME_TRADABLE.get(symbol, symbol),
                market=MARKET_CRYPTO,
                dedup_key=dedup_key,
                priority=PRIORITY_NORMAL,
                direction=DIRECTION_LONG if pattern == "POSITION_BUILDING" else DIRECTION_SHORT,
                payload={
                    "reference_symbol": symbol,
                    "pattern": pattern,
                    "oi_change_pct": round(oi_change * 100, 2),
                    "price_change_pct": round(price_change * 100, 2),
                    "window_hours": 24,
                },
            ))

        if events:
            self._logger.info("crypto_oi_events", count=len(events))
        return events
