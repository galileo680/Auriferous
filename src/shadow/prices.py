from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger("ShadowPrices")


def _fetch_sync(tickers: list[str]) -> dict[str, float]:
    import yfinance as yf

    prices: dict[str, float] = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).fast_info
            price = float(info["last_price"])
        except Exception as e:
            logger.warning("shadow_price_unavailable", ticker=ticker, error=str(e))
            continue
        if price > 0:
            prices[ticker] = price
    return prices


async def fetch_prices(tickers: list[str]) -> dict[str, float]:
    unique = sorted({t.upper() for t in tickers})
    if not unique:
        return {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_sync, unique)
