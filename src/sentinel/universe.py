from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import structlog

from src.core.config import UniverseConfig

logger = structlog.get_logger("Universe")

UNIVERSE_PATH = Path("data/universe_auriferous.csv")
FIELDNAMES = ("ticker", "name", "sector", "market_cap", "price", "dollar_volume", "option_oi", "cik")

SECTOR_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    name: str = ""
    sector: Optional[str] = None
    market_cap: Optional[float] = None
    price: Optional[float] = None
    dollar_volume: Optional[float] = None
    option_oi: Optional[int] = None
    cik: Optional[str] = None


def normalize_sector(value: str | None) -> str:
    cleaned = (value or "").strip()
    return cleaned if cleaned else SECTOR_UNKNOWN


def passes_filters(entry: UniverseEntry, config: UniverseConfig) -> tuple[bool, list[str]]:
    failures: list[str] = []

    if entry.market_cap is None:
        failures.append("market cap unknown")
    elif entry.market_cap < config.min_market_cap:
        failures.append("market cap below floor")
    elif entry.market_cap > config.max_market_cap:
        failures.append("market cap above ceiling")

    if entry.price is None:
        failures.append("price unknown")
    elif entry.price < config.min_price:
        failures.append("price below floor")
    elif entry.price > config.max_price:
        failures.append("price above ceiling")

    if entry.dollar_volume is None:
        failures.append("dollar volume unknown")
    elif entry.dollar_volume < config.min_dollar_volume:
        failures.append("dollar volume too thin")

    if entry.option_oi is not None and entry.option_oi < config.min_total_open_interest:
        failures.append("option open interest too thin")

    return not failures, failures


def load_universe(path: Path | str = UNIVERSE_PATH) -> list[UniverseEntry]:
    path = Path(path)
    if not path.exists():
        logger.warning("universe_missing", path=str(path))
        return []

    entries: list[UniverseEntry] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            entries.append(UniverseEntry(
                ticker=ticker,
                name=(row.get("name") or "").strip(),
                sector=normalize_sector(row.get("sector")),
                market_cap=_to_float(row.get("market_cap")),
                price=_to_float(row.get("price")),
                dollar_volume=_to_float(row.get("dollar_volume")),
                option_oi=_to_int(row.get("option_oi")),
                cik=(row.get("cik") or "").strip() or None,
            ))

    logger.info("universe_loaded", count=len(entries), path=str(path))
    return entries


def save_universe(entries: Iterable[UniverseEntry], path: Path | str = UNIVERSE_PATH) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "ticker": entry.ticker,
                "name": entry.name,
                "sector": entry.sector or "",
                "market_cap": entry.market_cap or "",
                "price": entry.price or "",
                "dollar_volume": entry.dollar_volume or "",
                "option_oi": entry.option_oi or "",
                "cik": entry.cik or "",
            })
            written += 1

    logger.info("universe_saved", count=written, path=str(path))
    return written


class UniverseIndex:

    def __init__(self, entries: list[UniverseEntry]) -> None:
        self._by_ticker = {e.ticker: e for e in entries}
        self._by_cik = {e.cik.lstrip("0"): e for e in entries if e.cik}

    def __len__(self) -> int:
        return len(self._by_ticker)

    def __contains__(self, ticker: str) -> bool:
        return ticker.upper() in self._by_ticker

    @property
    def tickers(self) -> list[str]:
        return sorted(self._by_ticker)

    def get(self, ticker: str) -> UniverseEntry | None:
        return self._by_ticker.get(ticker.upper())

    def by_cik(self, cik: str) -> UniverseEntry | None:
        return self._by_cik.get(str(cik).lstrip("0"))


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    result = _to_float(value)
    return int(result) if result is not None else None
