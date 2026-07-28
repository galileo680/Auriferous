from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from src.database.models import Event
from src.sentinel.universe import UniverseIndex

FILING_EXCERPT_CHARS = 6000
INSIDER_ROWS = 8
PRICE_WINDOWS = (1, 5, 20, 60)


@dataclass
class PriceSnapshot:
    last: Optional[float] = None
    changes_pct: dict[int, float] = field(default_factory=dict)
    atr_pct: Optional[float] = None
    range_52w_position: Optional[float] = None
    volume_ratio: Optional[float] = None
    gap_at_open_pct: Optional[float] = None

    def render(self) -> str:
        if self.last is None:
            return "- price history unavailable"

        lines = [f"- Last close: {self.last:.2f}"]
        for window in PRICE_WINDOWS:
            change = self.changes_pct.get(window)
            if change is not None:
                lines.append(f"- Change over {window} session(s): {change:+.2f}%")
        if self.atr_pct is not None:
            lines.append(f"- ATR(14) as % of price: {self.atr_pct:.2f}%")
        if self.range_52w_position is not None:
            lines.append(
                f"- Position in 52-week range: {self.range_52w_position:.0%} "
                f"(0 = at the low, 1 = at the high)"
            )
        if self.volume_ratio is not None:
            lines.append(f"- Volume vs 20-day average: {self.volume_ratio:.2f}x")
        if self.gap_at_open_pct is not None:
            lines.append(f"- Gap at the open: {self.gap_at_open_pct:+.2f}%")
        return "\n".join(lines)


@dataclass
class Fundamentals:
    market_cap: Optional[float] = None
    enterprise_value: Optional[float] = None
    revenue_ttm: Optional[float] = None
    gross_margin: Optional[float] = None
    total_cash: Optional[float] = None
    total_debt: Optional[float] = None
    shares_outstanding: Optional[float] = None
    short_percent_float: Optional[float] = None
    short_ratio: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None

    def render(self) -> str:
        def money(value: Optional[float]) -> str:
            if value is None:
                return "unavailable"
            if abs(value) >= 1_000_000_000:
                return f"${value / 1_000_000_000:.2f}B"
            return f"${value / 1_000_000:.0f}M"

        cash_runway = ""
        if self.total_cash is not None and self.revenue_ttm is not None:
            if self.revenue_ttm < self.total_cash * 0.2:
                cash_runway = "  (pre-revenue or early commercial — dilution risk is material)"

        return "\n".join([
            f"- Market cap: {money(self.market_cap)}",
            f"- Enterprise value: {money(self.enterprise_value)}",
            f"- Revenue TTM: {money(self.revenue_ttm)}{cash_runway}",
            f"- Gross margin: {f'{self.gross_margin:.1%}' if self.gross_margin is not None else 'unavailable'}",
            f"- Cash: {money(self.total_cash)}",
            f"- Debt: {money(self.total_debt)}",
            f"- Short interest as % of float: "
            f"{f'{self.short_percent_float:.1%}' if self.short_percent_float is not None else 'unavailable'}",
            f"- Days to cover: {f'{self.short_ratio:.1f}' if self.short_ratio is not None else 'unavailable'}",
            f"- Sector / industry: {self.sector or 'unknown'} / {self.industry or 'unknown'}",
        ])


@dataclass
class EvidenceBundle:
    ticker: str
    event_summary: str
    triage_summary: str
    price: PriceSnapshot = field(default_factory=PriceSnapshot)
    fundamentals: Fundamentals = field(default_factory=Fundamentals)
    filing_excerpt: Optional[str] = None
    insider_activity: Optional[str] = None
    after_hours_move_pct: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        blocks = [
            f"EVENT\n{self.event_summary}",
            f"\nTRIAGE ASSESSMENT\n{self.triage_summary}",
            f"\nPRICE ACTION\n{self.price.render()}",
            f"\nFUNDAMENTALS\n{self.fundamentals.render()}",
        ]
        if self.after_hours_move_pct is not None:
            blocks.append(
                f"\nEXTENDED HOURS\n- Move outside the regular session: "
                f"{self.after_hours_move_pct:+.2f}%"
            )
        if self.insider_activity:
            blocks.append(f"\nRECENT INSIDER TRANSACTIONS\n{self.insider_activity}")
        if self.filing_excerpt:
            blocks.append(f"\nFILING EXCERPT\n{self.filing_excerpt}")
        if self.warnings:
            blocks.append(f"\nDATA GAPS\n- " + "\n- ".join(self.warnings))
        return "\n".join(blocks)

    def priced_in_inputs(self) -> dict[str, Any]:
        return {
            "move_1d_pct": self.price.changes_pct.get(1),
            "move_5d_pct": self.price.changes_pct.get(5),
            "move_20d_pct": self.price.changes_pct.get(20),
            "after_hours_move_pct": self.after_hours_move_pct,
            "gap_at_open_pct": self.price.gap_at_open_pct,
            "volume_ratio": self.price.volume_ratio,
            "atr_pct": self.price.atr_pct,
            "range_52w_position": self.price.range_52w_position,
            "short_percent_float": self.fundamentals.short_percent_float,
            "days_to_cover": self.fundamentals.short_ratio,
            "iv_rank": None,
            "iv_vs_realized": None,
        }


class EvidenceCollector:

    def __init__(self, universe: UniverseIndex, fetch_filings: bool = True) -> None:
        self._universe = universe
        self._fetch_filings = fetch_filings
        self._logger = structlog.get_logger("EvidenceCollector")

    async def collect(self, event: Event) -> EvidenceBundle:
        bundle = EvidenceBundle(
            ticker=event.ticker,
            event_summary=self._summarize_event(event),
            triage_summary=self._summarize_triage(event),
        )

        triage = event.triage_result or {}
        context = triage.get("context") or {}
        bundle.after_hours_move_pct = context.get("after_hours_move_pct")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._attach_market_data, bundle)

        if self._fetch_filings:
            await self._attach_filing(bundle, event)

        return bundle

    @staticmethod
    def _summarize_event(event: Event) -> str:
        lines = [
            f"- Ticker: {event.ticker}",
            f"- Source: {event.source}",
            f"- Detected: {event.detected_at.isoformat() if event.detected_at else 'unknown'}",
        ]
        for key, value in (event.payload or {}).items():
            if value in (None, ""):
                continue
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value)
            text = str(value)
            lines.append(f"- {key}: {text[:400]}")
        return "\n".join(lines)

    @staticmethod
    def _summarize_triage(event: Event) -> str:
        result = (event.triage_result or {}).get("result") or {}
        if not result:
            return "- triage result unavailable"
        return "\n".join([
            f"- Catalyst type: {result.get('catalyst_type')}",
            f"- Preliminary direction: {result.get('direction')}",
            f"- Expected move: {result.get('expected_move_pct')}%",
            f"- Time to impact: {result.get('time_to_impact_hours')}h",
            f"- Triage reasoning: {result.get('reasoning')}",
        ])

    def _attach_market_data(self, bundle: EvidenceBundle) -> None:
        try:
            self._attach_prices(bundle)
        except Exception as e:
            bundle.warnings.append("price history unavailable")
            self._logger.warning("evidence_prices_failed", ticker=bundle.ticker, error=str(e))

        try:
            self._attach_fundamentals(bundle)
        except Exception as e:
            bundle.warnings.append("fundamentals unavailable")
            self._logger.warning("evidence_fundamentals_failed", ticker=bundle.ticker, error=str(e))

        try:
            self._attach_insiders(bundle)
        except Exception as e:
            self._logger.debug("evidence_insiders_failed", ticker=bundle.ticker, error=str(e))

    def _attach_prices(self, bundle: EvidenceBundle) -> None:
        import numpy as np
        import yfinance as yf

        frame = yf.Ticker(bundle.ticker).history(period="1y", interval="1d")
        if frame is None or frame.empty:
            bundle.warnings.append("no daily bars returned")
            return

        frame = frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        if len(frame) < 21:
            bundle.warnings.append("less than one month of history")
            return

        closes = frame["Close"].to_numpy(dtype=float)
        snapshot = bundle.price
        snapshot.last = float(closes[-1])

        for window in PRICE_WINDOWS:
            if len(closes) > window:
                reference = float(closes[-(window + 1)])
                if reference > 0:
                    snapshot.changes_pct[window] = (snapshot.last - reference) / reference * 100

        highs = frame["High"].to_numpy(dtype=float)[-15:]
        lows = frame["Low"].to_numpy(dtype=float)[-15:]
        prior = closes[-16:-1]
        if len(prior) == len(highs) == len(lows):
            true_range = np.maximum(
                highs - lows,
                np.maximum(np.abs(highs - prior), np.abs(lows - prior)),
            )
            atr = float(np.mean(true_range))
            if snapshot.last > 0:
                snapshot.atr_pct = atr / snapshot.last * 100

        year_high = float(frame["High"].max())
        year_low = float(frame["Low"].min())
        if year_high > year_low:
            snapshot.range_52w_position = (snapshot.last - year_low) / (year_high - year_low)

        volumes = frame["Volume"].to_numpy(dtype=float)
        average = float(np.mean(volumes[-21:-1]))
        if average > 0:
            snapshot.volume_ratio = float(volumes[-1]) / average

        previous_close = float(closes[-2])
        today_open = float(frame["Open"].iloc[-1])
        if previous_close > 0:
            snapshot.gap_at_open_pct = (today_open - previous_close) / previous_close * 100

    def _attach_fundamentals(self, bundle: EvidenceBundle) -> None:
        import yfinance as yf

        info = yf.Ticker(bundle.ticker).info or {}
        entry = self._universe.get(bundle.ticker)

        bundle.fundamentals = Fundamentals(
            market_cap=info.get("marketCap") or (entry.market_cap if entry else None),
            enterprise_value=info.get("enterpriseValue"),
            revenue_ttm=info.get("totalRevenue"),
            gross_margin=info.get("grossMargins"),
            total_cash=info.get("totalCash"),
            total_debt=info.get("totalDebt"),
            shares_outstanding=info.get("sharesOutstanding"),
            short_percent_float=info.get("shortPercentOfFloat"),
            short_ratio=info.get("shortRatio"),
            sector=info.get("sector") or (entry.sector if entry else None),
            industry=info.get("industry"),
        )

    def _attach_insiders(self, bundle: EvidenceBundle) -> None:
        import yfinance as yf

        frame = yf.Ticker(bundle.ticker).insider_transactions
        if frame is None or frame.empty:
            return

        rows: list[str] = []
        for _, row in frame.head(INSIDER_ROWS).iterrows():
            insider = str(row.get("Insider", "")).strip()
            text = str(row.get("Text", "") or row.get("Transaction", "")).strip()
            value = row.get("Value")
            start = row.get("Start Date")
            if not insider:
                continue
            amount = f", ${float(value):,.0f}" if value and str(value) != "nan" else ""
            rows.append(f"- {start}: {insider} — {text}{amount}")

        if rows:
            bundle.insider_activity = "\n".join(rows)

    async def _attach_filing(self, bundle: EvidenceBundle, event: Event) -> None:
        url = (event.payload or {}).get("index_url")
        if not url:
            return

        try:
            import httpx

            from src.dataflows.pdufa_harvester import strip_html

            async with httpx.AsyncClient(
                headers={"User-Agent": "Auriferous Trading System"}, timeout=20.0
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

            text = strip_html(response.text)
            bundle.filing_excerpt = text[:FILING_EXCERPT_CHARS]
        except Exception as e:
            bundle.warnings.append("filing text could not be retrieved")
            self._logger.debug("evidence_filing_failed", ticker=bundle.ticker, error=str(e))
