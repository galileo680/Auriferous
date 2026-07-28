from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import SentinelConfig, UniverseConfig
from src.database.models import Base, Event
from src.database.repositories import EventRepository
from src.sentinel.loop import SentinelLoop
from src.sentinel.models import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    DIRECTION_UNCLEAR,
    MARKET_EQUITY,
    PRIORITY_CRITICAL,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    EventSource,
    RawEvent,
)
from src.sentinel.sources.crypto_flow import (
    funding_direction,
    funding_is_extreme,
    oi_divergence,
)
from src.sentinel.sources.earnings import EarningsEntry, classify_proximity
from src.sentinel.sources.pdufa import PdufaEntry, in_window
from src.sentinel.sources.volume_anomaly import (
    AnomalyReading,
    compute_volume_ratio,
    direction_from_move,
    is_anomaly,
    session_fraction_elapsed,
)
from src.sentinel.universe import UniverseEntry, UniverseIndex, passes_filters


def make_entry(**overrides) -> UniverseEntry:
    defaults = dict(
        ticker="ACME",
        name="Acme Biosciences",
        market_cap=1_500_000_000.0,
        price=42.0,
        dollar_volume=12_000_000.0,
        option_oi=5000,
        cik="0001234567",
    )
    defaults.update(overrides)
    return UniverseEntry(**defaults)


def test_universe_accepts_a_typical_small_cap():
    ok, failures = passes_filters(make_entry(), UniverseConfig())
    assert ok
    assert failures == []


def test_universe_rejects_mega_cap():
    ok, failures = passes_filters(
        make_entry(market_cap=3_000_000_000_000.0), UniverseConfig()
    )
    assert not ok
    assert "market cap above ceiling" in failures


def test_universe_rejects_expensive_stock():
    ok, failures = passes_filters(make_entry(price=850.0), UniverseConfig())
    assert not ok
    assert "price above ceiling" in failures


def test_universe_rejects_illiquid_stock():
    ok, failures = passes_filters(make_entry(dollar_volume=200_000.0), UniverseConfig())
    assert not ok
    assert "dollar volume too thin" in failures


def test_universe_rejects_missing_data():
    ok, failures = passes_filters(make_entry(market_cap=None), UniverseConfig())
    assert not ok
    assert "market cap unknown" in failures


def test_universe_index_lookup_by_ticker_and_cik():
    index = UniverseIndex([make_entry(), make_entry(ticker="NWND", cik="0007654321")])
    assert "acme" in index
    assert len(index) == 2
    assert index.get("ACME").name == "Acme Biosciences"
    assert index.by_cik("7654321").ticker == "NWND"
    assert index.by_cik("0007654321").ticker == "NWND"
    assert index.by_cik("9999999") is None


def test_session_fraction_before_open_is_zero():
    assert session_fraction_elapsed(datetime(2026, 7, 27, 12, 0)) == 0.0


def test_session_fraction_after_close_is_one():
    assert session_fraction_elapsed(datetime(2026, 7, 27, 21, 0)) == 1.0


def test_session_fraction_midday():
    fraction = session_fraction_elapsed(datetime(2026, 7, 27, 17, 0))
    assert fraction == pytest.approx(0.5384, abs=0.001)


def test_volume_ratio_scales_by_session_progress():
    ratio = compute_volume_ratio(volume_today=500_000, avg_daily_volume=1_000_000, session_fraction=0.25)
    assert ratio == pytest.approx(2.0)


def test_volume_ratio_none_too_early_in_session():
    assert compute_volume_ratio(100, 1_000_000, 0.01) is None


def test_volume_ratio_none_without_history():
    assert compute_volume_ratio(100, 0, 0.5) is None


def test_anomaly_requires_both_volume_and_price_move():
    config = SentinelConfig()
    loud_but_flat = AnomalyReading("ACME", volume_ratio=8.0, price_move_atr=0.3, price_change_pct=0.5, last_price=42.0)
    moving_but_quiet = AnomalyReading("ACME", volume_ratio=1.2, price_move_atr=3.0, price_change_pct=9.0, last_price=42.0)
    both = AnomalyReading("ACME", volume_ratio=5.0, price_move_atr=2.2, price_change_pct=8.0, last_price=42.0)

    assert not is_anomaly(loud_but_flat, config)
    assert not is_anomaly(moving_but_quiet, config)
    assert is_anomaly(both, config)


def test_anomaly_triggers_on_downside_move_too():
    reading = AnomalyReading("ACME", volume_ratio=6.0, price_move_atr=-2.5, price_change_pct=-11.0, last_price=30.0)
    assert is_anomaly(reading, SentinelConfig())
    assert direction_from_move(reading.price_change_pct) == DIRECTION_SHORT


def test_direction_from_move():
    assert direction_from_move(4.0) == DIRECTION_LONG
    assert direction_from_move(-4.0) == DIRECTION_SHORT
    assert direction_from_move(0.0) == DIRECTION_UNCLEAR


def test_pdufa_window_covers_before_and_after():
    entry = PdufaEntry("ACME", date(2026, 8, 1), "drug", "indication", "NDA")
    window = (-5, 2)

    assert in_window(entry, date(2026, 7, 27), window)
    assert in_window(entry, date(2026, 8, 1), window)
    assert in_window(entry, date(2026, 8, 3), window)


def test_pdufa_window_excludes_outside_dates():
    entry = PdufaEntry("ACME", date(2026, 8, 1), "drug", "indication", "NDA")
    window = (-5, 2)

    assert not in_window(entry, date(2026, 7, 20), window)
    assert not in_window(entry, date(2026, 8, 10), window)


def test_earnings_proximity_classification():
    entry = EarningsEntry("ACME", date(2026, 8, 1))
    assert classify_proximity(entry, date(2026, 7, 30)) == "PRE_EVENT"
    assert classify_proximity(entry, date(2026, 8, 1)) == "POST_EVENT"
    assert classify_proximity(entry, date(2026, 7, 20)) is None
    assert classify_proximity(entry, date(2026, 8, 5)) is None


def test_funding_extreme_requires_consecutive_periods():
    assert not funding_is_extreme([0.0009, 0.0002, 0.0008], 0.0005, 3)
    assert funding_is_extreme([0.0009, 0.0008, 0.0007], 0.0005, 3)


def test_funding_extreme_works_for_negative_side():
    assert funding_is_extreme([-0.0009, -0.0008, -0.0007], 0.0005, 3)


def test_funding_extreme_rejects_mixed_signs():
    assert not funding_is_extreme([0.0009, -0.0008, 0.0007], 0.0005, 3)


def test_funding_extreme_needs_enough_history():
    assert not funding_is_extreme([0.0009, 0.0008], 0.0005, 3)


def test_funding_direction_is_contrarian():
    assert funding_direction(0.001) == DIRECTION_SHORT
    assert funding_direction(-0.001) == DIRECTION_LONG


def test_oi_divergence_detects_building_and_unwinding():
    assert oi_divergence(0.15, 0.005) == "POSITION_BUILDING"
    assert oi_divergence(-0.15, 0.005) == "POSITION_UNWINDING"


def test_oi_divergence_ignores_trending_price():
    assert oi_divergence(0.15, 0.08) is None


def test_oi_divergence_ignores_small_changes():
    assert oi_divergence(0.02, 0.001) is None


class StubSource(EventSource):

    def __init__(self, name: str, events: list[RawEvent], fail: bool = False) -> None:
        self.name = name
        self._events = events
        self._fail = fail
        self.closed = False

    async def poll(self) -> list[RawEvent]:
        if self._fail:
            raise RuntimeError("feed unreachable")
        return self._events

    async def close(self) -> None:
        self.closed = True


def make_raw(ticker: str = "ACME", key: str = "k1", priority: int = PRIORITY_NORMAL) -> RawEvent:
    return RawEvent(
        source="EDGAR_8K",
        ticker=ticker,
        market=MARKET_EQUITY,
        dedup_key=key,
        priority=priority,
        direction=DIRECTION_UNCLEAR,
        payload={"items": ["2.02"]},
    )


def run_with_db(body):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await body(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def test_batch_dedup_keeps_highest_priority_version():
    events = [
        make_raw(key="dup", priority=PRIORITY_LOW),
        make_raw(key="dup", priority=PRIORITY_CRITICAL),
        make_raw(key="other", priority=PRIORITY_NORMAL),
    ]
    deduped = SentinelLoop._deduplicate_in_batch(events)

    assert len(deduped) == 2
    assert deduped[0].priority == PRIORITY_CRITICAL
    by_key = {e.dedup_key: e for e in deduped}
    assert by_key["dup"].priority == PRIORITY_CRITICAL


def test_batch_dedup_sorts_by_priority():
    events = [make_raw(key="a", priority=PRIORITY_LOW), make_raw(key="b", priority=PRIORITY_CRITICAL)]
    assert [e.dedup_key for e in SentinelLoop._deduplicate_in_batch(events)] == ["b", "a"]


def test_raw_event_uppercases_ticker():
    assert make_raw(ticker="acme").ticker == "ACME"


def test_persist_writes_events_and_skips_repeats():
    async def body(session):
        from unittest.mock import patch

        loop = SentinelLoop([])
        repo = EventRepository(session)

        class FakeDB:
            def session(self):
                class Ctx:
                    async def __aenter__(inner):
                        return session

                    async def __aexit__(inner, *args):
                        return False

                return Ctx()

        with patch("src.sentinel.loop.DatabaseManager.get_instance", return_value=FakeDB()):
            first = await loop._persist([make_raw(key="k1"), make_raw(key="k2")])
            second = await loop._persist([make_raw(key="k1"), make_raw(key="k3")])

        return first, second, await repo.count()

    first, second, total = run_with_db(body)
    assert first == (2, 0)
    assert second == (1, 1)
    assert total == 3


def test_sentinel_isolates_a_failing_source():
    async def body(session):
        from unittest.mock import patch

        good = StubSource("good", [make_raw(key="ok")])
        bad = StubSource("bad", [], fail=True)
        loop = SentinelLoop([good, bad])

        class FakeDB:
            def session(self):
                class Ctx:
                    async def __aenter__(inner):
                        return session

                    async def __aexit__(inner, *args):
                        return False

                return Ctx()

        with patch("src.sentinel.loop.DatabaseManager.get_instance", return_value=FakeDB()):
            return await loop.run()

    result = run_with_db(body)
    assert result.errors == 1
    assert result.polled == 1
    assert result.persisted == 1
    assert result.by_source["good"] == 1


def test_sentinel_close_closes_every_source():
    sources = [StubSource("a", []), StubSource("b", [])]
    asyncio.run(SentinelLoop(sources).close())
    assert all(s.closed for s in sources)
