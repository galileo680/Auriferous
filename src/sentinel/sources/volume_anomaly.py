from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timezone

import structlog

from src.core.config import SentinelConfig
from src.sentinel.models import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    DIRECTION_UNCLEAR,
    MARKET_EQUITY,
    PRIORITY_NORMAL,
    SOURCE_VOLUME_ANOMALY,
    EventSource,
    RawEvent,
)
from src.sentinel.universe import UniverseIndex

BATCH_SIZE = 200
SESSION_OPEN = time(13, 30)
SESSION_CLOSE = time(20, 0)
SESSION_MINUTES = 390
MIN_SESSION_FRACTION = 0.05


@dataclass
class AnomalyReading:
    ticker: str
    volume_ratio: float
    price_move_atr: float
    price_change_pct: float
    last_price: float


def session_fraction_elapsed(now_utc: datetime) -> float:
    current = now_utc.time()
    if current <= SESSION_OPEN:
        return 0.0
    if current >= SESSION_CLOSE:
        return 1.0

    elapsed = (
        datetime.combine(now_utc.date(), current)
        - datetime.combine(now_utc.date(), SESSION_OPEN)
    ).total_seconds() / 60
    return min(max(elapsed / SESSION_MINUTES, 0.0), 1.0)


def compute_volume_ratio(
    volume_today: float,
    avg_daily_volume: float,
    session_fraction: float,
) -> float | None:
    if avg_daily_volume <= 0 or session_fraction < MIN_SESSION_FRACTION:
        return None
    expected = avg_daily_volume * session_fraction
    if expected <= 0:
        return None
    return volume_today / expected


def is_anomaly(reading: AnomalyReading, config: SentinelConfig) -> bool:
    return (
        reading.volume_ratio >= config.volume_anomaly_ratio
        and abs(reading.price_move_atr) >= config.volume_anomaly_atr_mult
    )


def direction_from_move(price_change_pct: float) -> str:
    if price_change_pct > 0:
        return DIRECTION_LONG
    if price_change_pct < 0:
        return DIRECTION_SHORT
    return DIRECTION_UNCLEAR


class VolumeAnomalySource(EventSource):

    name = "volume_anomaly"
    market = MARKET_EQUITY

    def __init__(self, universe: UniverseIndex, config: SentinelConfig) -> None:
        self._universe = universe
        self._config = config
        self._logger = structlog.get_logger("VolumeAnomalySource")
        self._seen_today: dict[str, str] = {}

    async def poll(self) -> list[RawEvent]:
        tickers = self._universe.tickers
        if not tickers:
            return []

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        fraction = session_fraction_elapsed(now)
        if fraction < MIN_SESSION_FRACTION:
            return []

        loop = asyncio.get_event_loop()
        readings = await loop.run_in_executor(
            None, self._scan_all, tickers, fraction
        )

        today_key = now.date().isoformat()
        events: list[RawEvent] = []

        for reading in readings:
            if not is_anomaly(reading, self._config):
                continue
            if self._seen_today.get(reading.ticker) == today_key:
                continue
            self._seen_today[reading.ticker] = today_key

            events.append(RawEvent(
                source=SOURCE_VOLUME_ANOMALY,
                ticker=reading.ticker,
                market=MARKET_EQUITY,
                dedup_key=f"volanom:{reading.ticker}:{today_key}",
                priority=PRIORITY_NORMAL,
                direction=direction_from_move(reading.price_change_pct),
                payload={
                    "volume_ratio": round(reading.volume_ratio, 2),
                    "price_move_atr": round(reading.price_move_atr, 2),
                    "price_change_pct": round(reading.price_change_pct, 2),
                    "last_price": round(reading.last_price, 2),
                    "session_fraction": round(fraction, 3),
                    "note": "unexplained activity — no filing detected yet",
                },
            ))

        if events:
            self._logger.info("volume_anomaly_events", count=len(events))
        return events

    def _scan_all(self, tickers: list[str], fraction: float) -> list[AnomalyReading]:
        readings: list[AnomalyReading] = []
        for start in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[start:start + BATCH_SIZE]
            try:
                readings.extend(self._scan_batch(batch, fraction))
            except Exception as e:
                self._logger.warning(
                    "volume_scan_batch_failed",
                    batch_start=start,
                    size=len(batch),
                    error=str(e),
                )
        return readings

    def _scan_batch(self, tickers: list[str], fraction: float) -> list[AnomalyReading]:
        import numpy as np
        import yfinance as yf

        data = yf.download(
            tickers=" ".join(tickers),
            period="1mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        if data is None or data.empty:
            return []

        readings: list[AnomalyReading] = []
        for ticker in tickers:
            try:
                frame = data[ticker] if len(tickers) > 1 else data
            except KeyError:
                continue

            frame = frame.dropna(subset=["Close", "Volume"])
            if len(frame) < 21:
                continue

            history = frame.iloc[:-1]
            today = frame.iloc[-1]

            avg_volume = float(history["Volume"].tail(20).mean())
            ratio = compute_volume_ratio(float(today["Volume"]), avg_volume, fraction)
            if ratio is None:
                continue

            highs = history["High"].tail(20).to_numpy()
            lows = history["Low"].tail(20).to_numpy()
            closes = history["Close"].tail(21).to_numpy()
            if len(closes) < 21:
                continue

            true_range = np.maximum(
                highs - lows,
                np.maximum(
                    np.abs(highs - closes[:-1]),
                    np.abs(lows - closes[:-1]),
                ),
            )
            atr = float(np.mean(true_range))
            if atr <= 0:
                continue

            previous_close = float(history["Close"].iloc[-1])
            last_price = float(today["Close"])
            change = last_price - previous_close

            readings.append(AnomalyReading(
                ticker=ticker,
                volume_ratio=ratio,
                price_move_atr=change / atr,
                price_change_pct=(change / previous_close * 100) if previous_close else 0.0,
                last_price=last_price,
            ))

        return readings
