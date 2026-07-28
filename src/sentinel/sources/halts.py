from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

from src.core.config import SentinelConfig
from src.sentinel.models import (
    DIRECTION_UNCLEAR,
    MARKET_EQUITY,
    SOURCE_HALT,
    EventSource,
    RawEvent,
    classify_halt,
)
from src.sentinel.universe import UniverseIndex

HALT_FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
REQUEST_TIMEOUT = 15.0
NDAQ_NS = {"ndaq": "http://www.nasdaqtrader.com/"}


@dataclass
class HaltRecord:
    symbol: str
    reason_code: str
    halt_date: str
    halt_time: str
    resumption_date: Optional[str]
    resumption_time: Optional[str]

    @property
    def dedup_key(self) -> str:
        return f"halt:{self.symbol}:{self.halt_date}:{self.halt_time}:{self.reason_code}"

    @property
    def resumed(self) -> bool:
        return bool(self.resumption_date and self.resumption_time)


def parse_halt_feed(xml_text: str) -> list[HaltRecord]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        structlog.get_logger("HaltSource").warning("halt_feed_parse_failed", error=str(e))
        return []

    records: list[HaltRecord] = []
    for item in root.iter("item"):
        symbol = _text(item, "IssueSymbol")
        reason = _text(item, "ReasonCode")
        if not symbol or not reason:
            continue

        records.append(HaltRecord(
            symbol=symbol.upper(),
            reason_code=reason.upper(),
            halt_date=_text(item, "HaltDate") or "",
            halt_time=_text(item, "HaltTime") or "",
            resumption_date=_text(item, "ResumptionDate"),
            resumption_time=_text(item, "ResumptionTradeTime"),
        ))

    return records


def _text(item: ET.Element, tag: str) -> Optional[str]:
    element = item.find(f"ndaq:{tag}", NDAQ_NS)
    if element is None:
        element = item.find(tag)
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


class HaltSource(EventSource):

    name = "nasdaq_halts"
    market = MARKET_EQUITY

    def __init__(self, universe: UniverseIndex, config: SentinelConfig) -> None:
        self._universe = universe
        self._config = config
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        self._logger = structlog.get_logger("HaltSource")
        self._seen: set[str] = set()

    async def close(self) -> None:
        await self._client.aclose()

    async def poll(self) -> list[RawEvent]:
        try:
            response = await self._client.get(HALT_FEED_URL)
            response.raise_for_status()
        except httpx.HTTPError as e:
            self._logger.warning("halt_feed_unavailable", error=str(e))
            return []

        events: list[RawEvent] = []
        for record in parse_halt_feed(response.text):
            if record.dedup_key in self._seen:
                continue
            self._seen.add(record.dedup_key)

            if record.symbol not in self._universe:
                continue
            if record.reason_code not in self._config.halt_codes:
                continue

            classification = classify_halt(record.reason_code)
            if classification is None:
                continue
            priority, meaning = classification

            events.append(RawEvent(
                source=SOURCE_HALT,
                ticker=record.symbol,
                market=MARKET_EQUITY,
                dedup_key=record.dedup_key,
                priority=priority,
                direction=DIRECTION_UNCLEAR,
                payload={
                    "reason_code": record.reason_code,
                    "meaning": meaning,
                    "halt_date": record.halt_date,
                    "halt_time": record.halt_time,
                    "resumed": record.resumed,
                    "resumption_time": record.resumption_time,
                },
            ))

        if events:
            self._logger.info("halt_events", count=len(events))
        return events
