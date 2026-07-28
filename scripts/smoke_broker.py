from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.broker.contracts import BFF_EXCHANGE, BFF_SYMBOL, future_spec, stock_spec
from src.broker.futures import FutureSelector
from src.broker.ibkr import IBKRClient
from src.broker.models import OptionRight, OrderSide
from src.broker.options import OptionSelector
from src.core.config import ConfigLoader

logger = structlog.get_logger("SmokeBroker")

PROBE_TICKERS = ["AMD", "PLTR", "SOFI"]


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def probe_account(broker: IBKRClient) -> float:
    summary = await broker.get_account_summary(force_refresh=True)
    logger.info(
        "account",
        total_equity=round(summary.total_equity, 2),
        cash=round(summary.cash_balance, 2),
        excess_liquidity=round(summary.excess_liquidity, 2),
    )
    return summary.total_equity


async def probe_stocks(broker: IBKRClient) -> None:
    specs = [stock_spec(t) for t in PROBE_TICKERS]
    quotes = await broker.get_quotes(specs)
    for ticker, quote in quotes.items():
        logger.info(
            "stock_quote",
            ticker=ticker,
            last=quote.last,
            bid=quote.bid,
            ask=quote.ask,
            spread_pct=round(quote.spread_pct, 4) if quote.spread_pct else None,
        )


async def probe_options(broker: IBKRClient, equity: float) -> None:
    config = ConfigLoader.get().structurer
    selector = OptionSelector(broker, config)

    for ticker in PROBE_TICKERS:
        try:
            expirations = await broker.get_option_expirations(ticker)
            logger.info(
                "option_expirations",
                ticker=ticker,
                count=len(expirations),
                first=expirations[:4],
            )
        except Exception as e:
            logger.error("option_expirations_failed", ticker=ticker, error=str(e))
            continue

        candidate = await selector.find_contract(
            symbol=ticker,
            right=OptionRight.CALL,
            equity=equity,
            event_date=date.today() + timedelta(days=10),
        )
        if candidate is None:
            logger.warning("option_no_candidate", ticker=ticker)
            continue

        margin = await broker.check_margin(
            spec=candidate.spec,
            side=OrderSide.BUY,
            quantity=1,
            limit_price=candidate.quote.mid,
        )
        logger.info(
            "option_candidate",
            ticker=ticker,
            contract=candidate.spec.describe(),
            delta=round(candidate.delta, 3) if candidate.delta else None,
            premium=round(candidate.premium, 2),
            spread_pct=round(candidate.liquidity.spread_pct, 4),
            open_interest=candidate.liquidity.open_interest,
            margin_accepted=margin.accepted,
            init_margin=round(margin.initial_margin_change, 2),
            commission=margin.commission,
        )


async def probe_futures(broker: IBKRClient) -> None:
    config = ConfigLoader.get()
    selector = FutureSelector(broker)

    try:
        expirations = await broker.get_future_expirations(BFF_SYMBOL, BFF_EXCHANGE)
        logger.info("bff_expirations", count=len(expirations), first=expirations[:5])
    except Exception as e:
        logger.error(
            "bff_expirations_failed",
            error=str(e),
            note="check CME market data subscription and BFF availability",
        )
        return

    spec = await broker.qualify(future_spec(BFF_SYMBOL, expirations[0], BFF_EXCHANGE))
    quote = await broker.get_quote(spec)
    logger.info("bff_quote", contract=spec.describe(), last=quote.last, bid=quote.bid, ask=quote.ask)

    if quote.last <= 0:
        logger.warning("bff_no_price", note="cannot size without a reference price")
        return

    candidate = await selector.find_bff(
        horizon_days=5,
        available_capital=config.capital.futures_bucket_usd,
        reference_price=quote.last,
    )
    if candidate is None:
        logger.warning("bff_no_candidate")
        return

    logger.info(
        "bff_candidate",
        contract=candidate.spec.describe(),
        initial_margin=round(candidate.initial_margin, 2),
        days_to_expiry=candidate.days_to_expiry,
        max_contracts=selector.max_contracts(
            config.capital.futures_bucket_usd, candidate.initial_margin
        ),
    )


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    _configure_logging(config.system.log_level)

    logger.info(
        "smoke_start",
        mode=config.system.mode,
        client_id=config.broker.client_id,
        market_data_type=config.broker.market_data_type,
        configured_capital=config.capital.initial_usd,
    )

    broker = IBKRClient(config.broker)
    await broker.connect()

    try:
        broker_equity = await probe_account(broker)
        equity = config.capital.initial_usd

        logger.info(
            "equity_basis",
            configured=equity,
            broker_reports=round(broker_equity, 2),
            note="risk sizing uses the configured value, never the broker balance",
        )

        await probe_stocks(broker)
        await probe_options(broker, equity)
        await probe_futures(broker)
    finally:
        await broker.disconnect()

    logger.info("smoke_complete")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path))
