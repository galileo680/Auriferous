from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger("PdufaHarvester")

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
REQUEST_TIMEOUT = 30.0
PAGE_SIZE = 100
MAX_PAGES = 10

SEARCH_TERMS = ('"PDUFA"', '"target action date"')
SEARCH_FORMS = ("8-K", "10-Q", "10-K")

KEYWORD_RE = re.compile(
    r"(PDUFA|target action date|goal date|action date)",
    re.IGNORECASE,
)
CONTEXT_CHARS = 400
MIN_ANNOUNCEMENT_GAP_DAYS = 7

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

LONG_DATE_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s+\(CIK")
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class PdufaHit:
    ticker: str
    company: str
    cik: str
    accession: str
    document: str
    filed_at: date

    @property
    def document_url(self) -> str:
        return f"{ARCHIVE_BASE}/{int(self.cik)}/{self.accession.replace('-', '')}/{self.document}"


@dataclass(frozen=True)
class PdufaDate:
    ticker: str
    pdufa_date: date
    drug: str
    indication: str
    phase: str
    source_accession: str


def extract_ticker(display_names: list[str]) -> Optional[str]:
    for name in display_names:
        match = TICKER_RE.search(name)
        if match:
            return match.group(1).upper()
    return None


def parse_search_hit(hit: dict[str, Any]) -> Optional[PdufaHit]:
    source = hit.get("_source") or {}
    identifier = hit.get("_id") or ""

    if ":" not in identifier:
        return None
    accession, _, document = identifier.partition(":")

    display_names = source.get("display_names") or []
    ticker = extract_ticker(display_names)
    if ticker is None:
        return None

    ciks = source.get("ciks") or []
    if not ciks:
        return None

    raw_date = source.get("file_date")
    try:
        filed_at = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

    company = display_names[0].split("(")[0].strip() if display_names else ""

    return PdufaHit(
        ticker=ticker,
        company=company,
        cik=str(ciks[0]),
        accession=accession,
        document=document,
        filed_at=filed_at,
    )


def strip_html(text: str) -> str:
    cleaned = TAG_RE.sub(" ", text)
    cleaned = cleaned.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", cleaned)


def find_dates_near_keyword(text: str, not_before: date) -> list[date]:
    found: list[date] = []

    for match in KEYWORD_RE.finditer(text):
        start = max(0, match.start() - CONTEXT_CHARS)
        window = text[start:match.end() + CONTEXT_CHARS]

        for long_match in LONG_DATE_RE.finditer(window):
            month = MONTHS[long_match.group(1).lower()]
            try:
                candidate = date(int(long_match.group(3)), month, int(long_match.group(2)))
            except ValueError:
                continue
            if candidate >= not_before:
                found.append(candidate)

        for numeric in NUMERIC_DATE_RE.finditer(window):
            try:
                candidate = date(
                    int(numeric.group(3)), int(numeric.group(1)), int(numeric.group(2))
                )
            except ValueError:
                continue
            if candidate >= not_before:
                found.append(candidate)

    return sorted(set(found))


def pick_pdufa_date(
    candidates: list[date],
    filed_at: date,
    horizon_days: int = 540,
    min_gap_days: int = MIN_ANNOUNCEMENT_GAP_DAYS,
) -> Optional[date]:
    floor = filed_at + timedelta(days=min_gap_days)
    horizon = filed_at + timedelta(days=horizon_days)
    plausible = [d for d in candidates if floor <= d <= horizon]
    return plausible[0] if plausible else None


class PdufaHarvester:

    def __init__(self, contact_email: str) -> None:
        if not contact_email:
            raise ValueError("SEC fair access policy requires a contact email")

        self._client = httpx.AsyncClient(
            headers={"User-Agent": f"Auriferous Trading System {contact_email}"},
            timeout=REQUEST_TIMEOUT,
        )
        self._logger = structlog.get_logger("PdufaHarvester")

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, lookback_days: int = 180) -> list[PdufaHit]:
        end = datetime.utcnow().date()
        start = end - timedelta(days=lookback_days)

        hits: dict[str, PdufaHit] = {}

        for term in SEARCH_TERMS:
            for form in SEARCH_FORMS:
                hits.update(await self._search_term(term, form, start, end))

        self._logger.info("pdufa_search_complete", filings=len(hits))
        return sorted(hits.values(), key=lambda h: h.filed_at, reverse=True)

    async def _search_term(
        self,
        term: str,
        form: str,
        start: date,
        end: date,
    ) -> dict[str, PdufaHit]:
        collected: dict[str, PdufaHit] = {}
        offset = 0

        for _ in range(MAX_PAGES):
            params = {
                "q": term,
                "forms": form,
                "dateRange": "custom",
                "startdt": start.isoformat(),
                "enddt": end.isoformat(),
                "from": offset,
                "size": PAGE_SIZE,
            }
            try:
                response = await self._client.get(SEARCH_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as e:
                self._logger.warning(
                    "pdufa_search_failed", term=term, form=form, error=str(e)
                )
                return collected

            rows = payload.get("hits", {}).get("hits", [])
            if not rows:
                break

            for row in rows:
                hit = parse_search_hit(row)
                if hit is not None:
                    collected[f"{hit.accession}:{hit.document}"] = hit

            total = payload.get("hits", {}).get("total", {}).get("value", 0)
            offset += PAGE_SIZE
            if offset >= total:
                break

        return collected

    async def extract_date(self, hit: PdufaHit) -> Optional[PdufaDate]:
        try:
            response = await self._client.get(hit.document_url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            self._logger.debug(
                "pdufa_document_failed", ticker=hit.ticker, error=str(e)
            )
            return None

        text = strip_html(response.text)
        candidates = find_dates_near_keyword(text, hit.filed_at)
        chosen = pick_pdufa_date(candidates, hit.filed_at)

        if chosen is None:
            return None

        return PdufaDate(
            ticker=hit.ticker,
            pdufa_date=chosen,
            drug="",
            indication="",
            phase="",
            source_accession=hit.accession,
        )

    async def harvest(self, lookback_days: int = 180) -> list[PdufaDate]:
        hits = await self.search(lookback_days)
        today = datetime.utcnow().date()

        best: dict[str, PdufaDate] = {}
        parsed = 0

        for hit in hits:
            result = await self.extract_date(hit)
            if result is None:
                continue
            parsed += 1

            if result.pdufa_date < today:
                continue

            existing = best.get(result.ticker)
            if existing is None or result.pdufa_date < existing.pdufa_date:
                best[result.ticker] = result

        self._logger.info(
            "pdufa_harvest_complete",
            filings_scanned=len(hits),
            dates_extracted=parsed,
            upcoming_dates=len(best),
        )
        return sorted(best.values(), key=lambda d: d.pdufa_date)
