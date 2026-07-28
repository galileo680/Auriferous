from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger("EdgarClient")

SEC_BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_BASE}/files/company_tickers.json"

MIN_REQUEST_INTERVAL = 0.12
MAX_RETRIES = 4
BACKOFF_BASE = 1.6
REQUEST_TIMEOUT = 20.0

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
CIK_RE = re.compile(r"/data/(\d+)/")
ITEM_RE = re.compile(r"Item\s+(\d{1,2}\.\d{2})", re.IGNORECASE)


@dataclass
class FilingRef:
    form_type: str
    cik: str
    accession: str
    company: str
    filed_at: Optional[datetime]
    index_url: str

    @property
    def dedup_key(self) -> str:
        return f"edgar:{self.accession}"


def parse_current_feed(xml_text: str) -> list[FilingRef]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("edgar_feed_parse_failed", error=str(e))
        return []

    filings: list[FilingRef] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        link_element = entry.find("atom:link", ATOM_NS)
        href = link_element.get("href") if link_element is not None else None
        if not href:
            continue

        accession_match = ACCESSION_RE.search(href) or ACCESSION_RE.search(title)
        cik_match = CIK_RE.search(href)
        if not accession_match or not cik_match:
            continue

        form_type, company = _split_title(title)
        updated = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)

        filings.append(FilingRef(
            form_type=form_type,
            cik=cik_match.group(1),
            accession=accession_match.group(1),
            company=company,
            filed_at=_parse_iso(updated),
            index_url=href if href.startswith("http") else f"{SEC_BASE}{href}",
        ))

    return filings


def parse_items(index_html: str) -> list[str]:
    found = {match.group(1) for match in ITEM_RE.finditer(index_html)}
    return sorted(found)


def _split_title(title: str) -> tuple[str, str]:
    if " - " in title:
        form_type, _, remainder = title.partition(" - ")
        company = remainder.split("(")[0].strip()
        return form_type.strip(), company
    return title.strip(), ""


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


class EdgarClient:

    def __init__(self, contact_email: str, cache_dir: Path | str = "data/cache") -> None:
        if not contact_email:
            raise ValueError("SEC fair access policy requires a contact email in User-Agent")

        self._headers = {
            "User-Agent": f"Auriferous Trading System {contact_email}",
            "Accept-Encoding": "gzip, deflate",
        }
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._client = httpx.AsyncClient(headers=self._headers, timeout=REQUEST_TIMEOUT)
        self._cik_to_ticker: dict[str, str] = {}
        self._logger = structlog.get_logger("EdgarClient")

    async def close(self) -> None:
        await self._client.aclose()

    def _acquire_slot(self) -> None:
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - elapsed)
            self._last_request = time.monotonic()

    async def _get(self, url: str) -> str | None:
        for attempt in range(MAX_RETRIES):
            self._acquire_slot()
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as e:
                self._logger.warning("edgar_request_error", url=url, error=str(e))
                time.sleep(BACKOFF_BASE ** attempt)
                continue

            if response.status_code == 200:
                return response.text

            if response.status_code in (403, 429, 503):
                wait = BACKOFF_BASE ** (attempt + 1)
                self._logger.warning(
                    "edgar_throttled",
                    url=url,
                    status=response.status_code,
                    retry_in=round(wait, 2),
                )
                time.sleep(wait)
                continue

            if response.status_code == 404:
                return None

            self._logger.warning("edgar_unexpected_status", url=url, status=response.status_code)
            return None

        self._logger.error("edgar_request_exhausted", url=url)
        return None

    async def load_ticker_map(self, max_age_hours: int = 24) -> dict[str, str]:
        if self._cik_to_ticker:
            return self._cik_to_ticker

        cache_file = self._cache_dir / "company_tickers.json"
        payload: dict[str, Any] | None = None

        if cache_file.exists():
            age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
            if age_hours < max_age_hours:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))

        if payload is None:
            text = await self._get(COMPANY_TICKERS_URL)
            if text is None:
                self._logger.error("edgar_ticker_map_unavailable")
                return {}
            payload = json.loads(text)
            cache_file.write_text(json.dumps(payload), encoding="utf-8")

        mapping: dict[str, str] = {}
        for record in payload.values():
            cik = str(record.get("cik_str") or "").lstrip("0")
            ticker = (record.get("ticker") or "").strip().upper()
            if cik and ticker and cik not in mapping:
                mapping[cik] = ticker

        self._cik_to_ticker = mapping
        self._logger.info("edgar_ticker_map_loaded", count=len(mapping))
        return mapping

    def ticker_for_cik(self, cik: str) -> str | None:
        return self._cik_to_ticker.get(str(cik).lstrip("0"))

    async def fetch_current_filings(self, form_type: str, count: int = 100) -> list[FilingRef]:
        url = (
            f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={form_type}&company=&dateb=&owner=include"
            f"&count={count}&output=atom"
        )
        text = await self._get(url)
        if text is None:
            return []
        return parse_current_feed(text)

    async def fetch_items(self, filing: FilingRef) -> list[str]:
        text = await self._get(filing.index_url)
        if text is None:
            return []
        return parse_items(text)

    async def fetch_document(self, url: str) -> str | None:
        return await self._get(url)
