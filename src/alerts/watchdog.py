from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from src.alerts.service import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    AlertService,
)
from src.broker.interface import BrokerInterface
from src.database.models import DRAWDOWN_DEFENSIVE, DRAWDOWN_HALT
from src.database.repositories import EquityRepository, ErrorRepository
from src.database.session import DatabaseManager
from src.positions.models import BLOCKING_ERROR_TYPES

ALERTED_STATES = (DRAWDOWN_DEFENSIVE, DRAWDOWN_HALT)


@dataclass
class WatchdogResult:
    alerts_sent: int = 0
    drawdown_state: Optional[str] = None
    blocking_errors: int = 0
    broker_connected: Optional[bool] = None


class WatchdogLoop:

    def __init__(
        self,
        alerts: AlertService,
        broker: Optional[BrokerInterface] = None,
    ) -> None:
        self._alerts = alerts
        self._broker = broker
        self._logger = structlog.get_logger("Watchdog")
        self._last_alerted_state: Optional[str] = None
        self._alerted_error_ids: set[int] = set()
        self._broker_was_connected: Optional[bool] = None

    async def run(self) -> WatchdogResult:
        result = WatchdogResult()
        db = DatabaseManager.get_instance()

        async with db.session() as session:
            latest = await EquityRepository(session).get_latest()
            state = latest.drawdown_state if latest else None
            result.drawdown_state = state

            if state in ALERTED_STATES and state != self._last_alerted_state:
                severity = (
                    SEVERITY_CRITICAL if state == DRAWDOWN_HALT else SEVERITY_WARNING
                )
                note = (
                    "no new positions — manual reset required (scripts/reset_halt.py)"
                    if state == DRAWDOWN_HALT
                    else "sizing cut to 25%, conviction floor 0.75"
                )
                await self._alerts.send(
                    severity,
                    f"drawdown state {state}",
                    f"drawdown {float(latest.drawdown_pct):.1%} from the high-water mark; {note}",
                )
                result.alerts_sent += 1
            self._last_alerted_state = state

            blocking = await ErrorRepository(session).unresolved(BLOCKING_ERROR_TYPES)
            result.blocking_errors = len(blocking)
            for error in blocking:
                if error.id in self._alerted_error_ids:
                    continue
                await self._alerts.send(
                    SEVERITY_CRITICAL,
                    f"blocking error: {error.error_type}",
                    f"{error.message}\nresolve, then run scripts/resolve_errors.py",
                )
                self._alerted_error_ids.add(error.id)
                result.alerts_sent += 1

        if self._broker is not None:
            connected = self._broker.is_connected()
            result.broker_connected = connected

            if self._broker_was_connected and not connected:
                await self._alerts.send(
                    SEVERITY_CRITICAL,
                    "broker disconnected",
                    "IB Gateway connection lost — open positions are unmanaged "
                    "until it returns",
                )
                result.alerts_sent += 1
            elif self._broker_was_connected is False and connected:
                await self._alerts.send(
                    SEVERITY_INFO,
                    "broker reconnected",
                    "IB Gateway connection restored",
                )
                result.alerts_sent += 1

            self._broker_was_connected = connected

        return result
