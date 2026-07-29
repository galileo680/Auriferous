from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alerts.service import SEVERITY_CRITICAL, AlertService
from src.alerts.watchdog import WatchdogLoop
from src.core.clock import utcnow_naive
from src.core.config import AlertsConfig, AuriferousConfig
from src.database.models import (
    DRAWDOWN_HALT,
    DRAWDOWN_NORMAL,
    Base,
    EquityCurve,
    ErrorLog,
    ShadowTrade,
    Trade,
)
from src.positions.models import ERROR_RECONCILE_MISMATCH
from src.shadow.book import BOOK_SHADOW, ORIGIN_REDTEAM_VETO
from scripts.validate import evaluate, kill_criteria


class RecordingAlerts(AlertService):

    def __init__(self, enabled: bool = True, webhook: str | None = "https://hook") -> None:
        async def transport(url, payload):
            self.delivered.append(payload)
            return True

        super().__init__(
            AlertsConfig(enabled=enabled, webhook_url=webhook),
            transport=transport,
        )
        self.delivered: list[dict] = []
        self.sent: list[tuple[str, str]] = []

    async def send(self, severity: str, title: str, message: str) -> bool:
        self.sent.append((severity, title))
        return await super().send(severity, title, message)


class ToggleBroker:

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


def run_db(work):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def equity_row(state: str, drawdown: float = 0.0) -> EquityCurve:
    return EquityCurve(
        equity=Decimal("2000"),
        high_water_mark=Decimal("2450"),
        drawdown_pct=Decimal(str(drawdown)),
        drawdown_state=state,
        realized_pnl=Decimal("0"),
        open_premium=Decimal("0"),
        open_positions=0,
    )


def closed_shadow(pnl: float, origin: str = ORIGIN_REDTEAM_VETO) -> ShadowTrade:
    return ShadowTrade(
        ticker="AAA",
        book=BOOK_SHADOW,
        origin=origin,
        catalyst_type="FDA_DECISION",
        conviction=Decimal("0.7"),
        entry_price=Decimal("30"),
        quantity=33,
        expected_holding_days=10,
        status="CLOSED",
        opened_at=utcnow_naive() - timedelta(days=12),
        closed_at=utcnow_naive(),
        pnl_virtual=Decimal(str(pnl)),
    )


def test_alert_service_logs_only_without_a_webhook():
    alerts = RecordingAlerts(webhook=None)
    delivered = asyncio.run(alerts.send(SEVERITY_CRITICAL, "t", "m"))
    assert delivered is False
    assert alerts.delivered == []


def test_alert_service_delivers_through_the_transport():
    alerts = RecordingAlerts()
    delivered = asyncio.run(alerts.send(SEVERITY_CRITICAL, "halt", "details"))
    assert delivered is True
    assert "halt" in alerts.delivered[0]["content"]


def run_watchdog(seed_objects, broker=None, runs: int = 1, toggle=None):
    async def work(session):
        for obj in seed_objects:
            session.add(obj)
        await session.commit()

        class FakeDB:
            def session(self):
                class Ctx:
                    async def __aenter__(inner):
                        return session

                    async def __aexit__(inner, *args):
                        await session.commit()
                        return False

                return Ctx()

        alerts = RecordingAlerts()
        watchdog = WatchdogLoop(alerts, broker)
        results = []
        with patch(
            "src.alerts.watchdog.DatabaseManager.get_instance",
            return_value=FakeDB(),
        ):
            for index in range(runs):
                if toggle is not None and index in toggle:
                    broker.connected = toggle[index]
                results.append(await watchdog.run())
        return results, alerts

    return run_db(work)


def test_watchdog_alerts_on_halt_exactly_once():
    results, alerts = run_watchdog([equity_row(DRAWDOWN_HALT, 0.35)], runs=2)

    assert results[0].alerts_sent == 1
    assert results[1].alerts_sent == 0
    assert alerts.sent[0][0] == SEVERITY_CRITICAL
    assert "HALT" in alerts.sent[0][1]


def test_watchdog_stays_quiet_in_normal_state():
    results, alerts = run_watchdog([equity_row(DRAWDOWN_NORMAL)], runs=1)
    assert results[0].alerts_sent == 0
    assert alerts.sent == []


def test_watchdog_alerts_each_blocking_error_once():
    error = ErrorLog(
        component="ReconcileLoop",
        error_type=ERROR_RECONCILE_MISMATCH,
        message="positions diverge",
    )
    results, alerts = run_watchdog([equity_row(DRAWDOWN_NORMAL), error], runs=2)

    assert results[0].alerts_sent == 1
    assert results[1].alerts_sent == 0
    assert "RECONCILE_MISMATCH" in alerts.sent[0][1]


def test_watchdog_alerts_on_disconnect_and_recovery():
    broker = ToggleBroker(connected=True)
    results, alerts = run_watchdog(
        [equity_row(DRAWDOWN_NORMAL)],
        broker=broker,
        runs=3,
        toggle={1: False, 2: True},
    )

    assert results[0].alerts_sent == 0
    assert results[1].alerts_sent == 1
    assert results[2].alerts_sent == 1
    assert "disconnected" in alerts.sent[0][1]
    assert "reconnected" in alerts.sent[1][1]


def test_validation_reports_insufficient_data_on_an_empty_book():
    async def work(session):
        return await evaluate(session, AuriferousConfig())

    criteria = run_db(work)
    by_name = {c.name: c for c in criteria}

    assert by_name["closed decisions >= 60"].passed is False
    assert by_name["no unresolved critical errors"].passed is True
    assert by_name["triage precision >= 25%"].passed is None


def test_validation_flags_a_positive_redteam_veto_value():
    async def work(session):
        session.add_all([closed_shadow(120.0), closed_shadow(60.0)])
        await session.flush()
        return await evaluate(session, AuriferousConfig())

    criteria = run_db(work)
    redteam = next(c for c in criteria if "REDTEAM" in c.name)
    assert redteam.passed is False


def test_validation_fails_on_entries_made_in_halt():
    async def work(session):
        session.add(Trade(
            ticker="AAA",
            market="EQUITY",
            instrument="OPTION",
            direction="LONG",
            contract_spec={"multiplier": 100},
            quantity=1,
            capital_at_risk=Decimal("100"),
            status="CLOSED",
            drawdown_state_at_entry=DRAWDOWN_HALT,
        ))
        await session.flush()
        return await evaluate(session, AuriferousConfig())

    criteria = run_db(work)
    halt = next(c for c in criteria if "HALT" in c.name)
    assert halt.passed is False


def test_kill_criterion_triggers_on_a_deep_drawdown():
    async def work(session):
        session.add(equity_row(DRAWDOWN_HALT, drawdown=0.45))
        await session.flush()
        return await kill_criteria(session, AuriferousConfig())

    kills = run_db(work)
    drawdown = next(c for c in kills if "drawdown" in c.name)
    assert drawdown.passed is True


def test_kill_criteria_stay_silent_on_a_healthy_book():
    async def work(session):
        session.add(equity_row(DRAWDOWN_NORMAL, drawdown=0.05))
        session.add_all([closed_shadow(-40.0), closed_shadow(60.0)])
        await session.flush()
        return await kill_criteria(session, AuriferousConfig())

    kills = run_db(work)
    assert all(c.passed is False for c in kills)
