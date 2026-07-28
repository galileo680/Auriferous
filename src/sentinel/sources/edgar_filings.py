from __future__ import annotations

import structlog

from src.core.config import SentinelConfig
from src.dataflows.edgar import EdgarClient, FilingRef
from src.sentinel.models import (
    DILUTION_FORMS,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    MARKET_EQUITY,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    SOURCE_EDGAR_13D,
    SOURCE_EDGAR_8K,
    SOURCE_EDGAR_DILUTION,
    EventSource,
    RawEvent,
    classify_edgar_items,
)
from src.sentinel.universe import UniverseIndex

FEED_COUNT = 100


class EdgarFilingSource(EventSource):

    name = "edgar_filings"
    market = MARKET_EQUITY

    def __init__(
        self,
        client: EdgarClient,
        universe: UniverseIndex,
        config: SentinelConfig,
    ) -> None:
        self._client = client
        self._universe = universe
        self._config = config
        self._logger = structlog.get_logger("EdgarFilingSource")
        self._seen: set[str] = set()

    async def poll(self) -> list[RawEvent]:
        await self._client.load_ticker_map()

        events: list[RawEvent] = []
        events.extend(await self._poll_8k())
        events.extend(await self._poll_dilution())
        events.extend(await self._poll_13d())
        return events

    def _resolve_ticker(self, filing: FilingRef) -> str | None:
        ticker = self._client.ticker_for_cik(filing.cik)
        if ticker and ticker in self._universe:
            return ticker

        entry = self._universe.by_cik(filing.cik)
        return entry.ticker if entry else None

    async def _poll_8k(self) -> list[RawEvent]:
        filings = await self._client.fetch_current_filings("8-K", FEED_COUNT)
        events: list[RawEvent] = []

        for filing in filings:
            if filing.accession in self._seen:
                continue

            ticker = self._resolve_ticker(filing)
            if ticker is None:
                self._seen.add(filing.accession)
                continue

            items = await self._client.fetch_items(filing)
            self._seen.add(filing.accession)

            relevant = [item for item in items if item in self._config.edgar_items]
            if not relevant:
                continue

            priority, direction, ranked = classify_edgar_items(relevant)

            events.append(RawEvent(
                source=SOURCE_EDGAR_8K,
                ticker=ticker,
                market=MARKET_EQUITY,
                dedup_key=filing.dedup_key,
                priority=priority,
                direction=direction,
                payload={
                    "form_type": filing.form_type,
                    "items": ranked,
                    "all_items": items,
                    "company": filing.company,
                    "accession": filing.accession,
                    "index_url": filing.index_url,
                    "filed_at": filing.filed_at.isoformat() if filing.filed_at else None,
                },
            ))

        if events:
            self._logger.info("edgar_8k_events", count=len(events))
        return events

    async def _poll_dilution(self) -> list[RawEvent]:
        events: list[RawEvent] = []

        for form_type in DILUTION_FORMS:
            for filing in await self._client.fetch_current_filings(form_type, FEED_COUNT):
                if filing.accession in self._seen:
                    continue

                ticker = self._resolve_ticker(filing)
                self._seen.add(filing.accession)
                if ticker is None:
                    continue

                events.append(RawEvent(
                    source=SOURCE_EDGAR_DILUTION,
                    ticker=ticker,
                    market=MARKET_EQUITY,
                    dedup_key=filing.dedup_key,
                    priority=PRIORITY_HIGH,
                    direction=DIRECTION_SHORT,
                    payload={
                        "form_type": filing.form_type,
                        "company": filing.company,
                        "accession": filing.accession,
                        "index_url": filing.index_url,
                        "note": "registration or prospectus filing — supply pressure",
                    },
                ))

        if events:
            self._logger.info("edgar_dilution_events", count=len(events))
        return events

    async def _poll_13d(self) -> list[RawEvent]:
        events: list[RawEvent] = []

        for form_type, priority in (("SC 13D", PRIORITY_HIGH), ("SC 13G", PRIORITY_LOW)):
            for filing in await self._client.fetch_current_filings(form_type, FEED_COUNT):
                if filing.accession in self._seen:
                    continue

                ticker = self._resolve_ticker(filing)
                self._seen.add(filing.accession)
                if ticker is None:
                    continue

                events.append(RawEvent(
                    source=SOURCE_EDGAR_13D,
                    ticker=ticker,
                    market=MARKET_EQUITY,
                    dedup_key=filing.dedup_key,
                    priority=priority,
                    direction=DIRECTION_LONG,
                    payload={
                        "form_type": filing.form_type,
                        "company": filing.company,
                        "accession": filing.accession,
                        "index_url": filing.index_url,
                        "activist": form_type == "SC 13D",
                    },
                ))

        if events:
            self._logger.info("edgar_13d_events", count=len(events))
        return events
