from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import ConfigLoader, UniverseConfig
from src.dataflows.edgar import EdgarClient
from src.sentinel.universe import (
    SECTOR_UNKNOWN,
    UNIVERSE_PATH,
    UniverseEntry,
    normalize_sector,
    passes_filters,
    save_universe,
)

logger = structlog.get_logger("RefreshUniverse")

WORKERS = 12
PROGRESS_EVERY = 250
CACHE_PATH = Path("data/cache/universe_probe.json")


def probe_ticker(ticker: str, cik: str, name: str) -> UniverseEntry | None:
    import yfinance as yf

    try:
        info = yf.Ticker(ticker).fast_info

        price = float(info.last_price or 0)
        market_cap = float(info.market_cap or 0)
        volume = float(info.last_volume or 0)
        if price <= 0 or market_cap <= 0:
            return None

        return UniverseEntry(
            ticker=ticker,
            name=name,
            sector=None,
            market_cap=market_cap,
            price=price,
            dollar_volume=price * volume,
            option_oi=None,
            cik=cik,
        )
    except Exception:
        return None


def probe_sector(ticker: str) -> str:
    import yfinance as yf

    try:
        return normalize_sector(yf.Ticker(ticker).info.get("sector"))
    except Exception:
        return SECTOR_UNKNOWN


def attach_sectors(entries: list[UniverseEntry]) -> list[UniverseEntry]:
    logger.info("sector_pass_start", tickers=len(entries))

    resolved: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(probe_sector, e.ticker): e.ticker for e in entries}
        for done, future in enumerate(as_completed(futures), start=1):
            resolved[futures[future]] = future.result()
            if done % PROGRESS_EVERY == 0:
                logger.info("sector_pass_progress", done=done, total=len(entries))

    unknown = sum(1 for s in resolved.values() if s == SECTOR_UNKNOWN)
    logger.info(
        "sector_pass_complete",
        resolved=len(resolved) - unknown,
        unknown=unknown,
        note="UNKNOWN entries share one bucket in the sector exposure limit",
    )

    return [
        UniverseEntry(
            ticker=e.ticker,
            name=e.name,
            sector=resolved.get(e.ticker, SECTOR_UNKNOWN),
            market_cap=e.market_cap,
            price=e.price,
            dollar_volume=e.dollar_volume,
            option_oi=e.option_oi,
            cik=e.cik,
        )
        for e in entries
    ]


def load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


async def build_seed_list(client: EdgarClient) -> list[tuple[str, str, str]]:
    cache_file = Path("data/cache/company_tickers.json")
    await client.load_ticker_map()

    if not cache_file.exists():
        raise RuntimeError("SEC ticker map unavailable — cannot seed the universe")

    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    seeds: list[tuple[str, str, str]] = []
    for record in payload.values():
        ticker = (record.get("ticker") or "").strip().upper()
        cik = str(record.get("cik_str") or "").lstrip("0")
        name = (record.get("title") or "").strip()
        if ticker and cik and "." not in ticker:
            seeds.append((ticker, cik, name))

    logger.info("seed_list_built", count=len(seeds))
    return seeds


def screen(
    seeds: list[tuple[str, str, str]],
    config: UniverseConfig,
    use_cache: bool,
) -> list[UniverseEntry]:
    cache = load_cache() if use_cache else {}
    accepted: list[UniverseEntry] = []
    probed = 0

    pending = [s for s in seeds if s[0] not in cache]
    logger.info("screening_start", total=len(seeds), cached=len(cache), to_probe=len(pending))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(probe_ticker, ticker, cik, name): ticker
            for ticker, cik, name in pending
        }
        for future in as_completed(futures):
            ticker = futures[future]
            probed += 1
            entry = future.result()
            cache[ticker] = (
                {
                    "name": entry.name,
                    "market_cap": entry.market_cap,
                    "price": entry.price,
                    "dollar_volume": entry.dollar_volume,
                    "cik": entry.cik,
                }
                if entry
                else {}
            )
            if probed % PROGRESS_EVERY == 0:
                logger.info("screening_progress", probed=probed, remaining=len(pending) - probed)
                save_cache(cache)

    save_cache(cache)

    for ticker, record in cache.items():
        if not record:
            continue
        entry = UniverseEntry(
            ticker=ticker,
            name=record.get("name", ""),
            market_cap=record.get("market_cap"),
            price=record.get("price"),
            dollar_volume=record.get("dollar_volume"),
            cik=record.get("cik"),
        )
        ok, _ = passes_filters(entry, config)
        if ok:
            accepted.append(entry)

    return sorted(accepted, key=lambda e: e.ticker)


async def main(config_path: str = "config/auriferous.yaml", fresh: bool = False) -> None:
    config = ConfigLoader.load(config_path=config_path)
    client = EdgarClient(config.sentinel.contact_email)

    try:
        seeds = await build_seed_list(client)
    finally:
        await client.close()

    accepted = screen(seeds, config.universe, use_cache=not fresh)
    accepted = attach_sectors(accepted)
    written = save_universe(accepted, UNIVERSE_PATH)

    logger.info(
        "universe_refresh_complete",
        candidates=len(seeds),
        accepted=written,
        path=str(UNIVERSE_PATH),
        filters={
            "market_cap": [config.universe.min_market_cap, config.universe.max_market_cap],
            "price": [config.universe.min_price, config.universe.max_price],
            "min_dollar_volume": config.universe.min_dollar_volume,
        },
    )


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path, fresh="--fresh" in sys.argv))
