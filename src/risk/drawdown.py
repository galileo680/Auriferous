from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import RiskConfig
from src.database.models import (
    DRAWDOWN_CAUTION,
    DRAWDOWN_DEFENSIVE,
    DRAWDOWN_HALT,
    DRAWDOWN_NORMAL,
    EquityCurve,
)
from src.database.repositories.equity import EquityRepository, classify_drawdown
from src.database.repositories.trade import TradeRepository

STATE_ORDER = [DRAWDOWN_NORMAL, DRAWDOWN_CAUTION, DRAWDOWN_DEFENSIVE, DRAWDOWN_HALT]


def entry_thresholds(config: RiskConfig) -> dict[str, float]:
    return {
        DRAWDOWN_NORMAL: 0.0,
        DRAWDOWN_CAUTION: config.drawdown_caution,
        DRAWDOWN_DEFENSIVE: config.drawdown_defensive,
        DRAWDOWN_HALT: config.drawdown_halt,
    }


def recovery_boundary(state: str, config: RiskConfig) -> float:
    thresholds = entry_thresholds(config)
    index = STATE_ORDER.index(state)
    if index == 0:
        return 0.0
    lower = thresholds[STATE_ORDER[index - 1]]
    return (lower + thresholds[state]) / 2.0


def next_state(previous: str, drawdown_pct: float, config: RiskConfig) -> str:
    if previous == DRAWDOWN_HALT:
        return DRAWDOWN_HALT

    raw = classify_drawdown(
        drawdown_pct,
        config.drawdown_caution,
        config.drawdown_defensive,
        config.drawdown_halt,
    )
    if STATE_ORDER.index(raw) >= STATE_ORDER.index(previous):
        return raw

    current = previous
    while current != DRAWDOWN_NORMAL:
        if drawdown_pct > recovery_boundary(current, config):
            break
        current = STATE_ORDER[STATE_ORDER.index(current) - 1]
        if current == raw:
            break
    return current


@dataclass(frozen=True)
class DrawdownSnapshot:
    equity: float
    high_water_mark: float
    drawdown_pct: float
    state: str
    previous_state: str


class DrawdownTracker:

    def __init__(self, config: RiskConfig, initial_equity: float) -> None:
        self._config = config
        self._initial = initial_equity
        self._logger = structlog.get_logger("DrawdownTracker")

    async def snapshot(self, session: AsyncSession, persist: bool = True) -> DrawdownSnapshot:
        trades = TradeRepository(session)
        equity_repo = EquityRepository(session)

        realized = float(await trades.realized_pnl_total())
        equity = self._initial + realized

        latest = await equity_repo.get_latest()
        previous_state = latest.drawdown_state if latest else DRAWDOWN_NORMAL
        baseline = float(latest.high_water_mark) if latest else self._initial
        hwm = max(baseline, equity)

        drawdown = (hwm - equity) / hwm if hwm > 0 else 0.0
        state = next_state(previous_state, drawdown, self._config)

        if state != previous_state:
            self._logger.warning(
                "drawdown_state_changed",
                previous=previous_state,
                state=state,
                drawdown_pct=round(drawdown, 4),
                equity=round(equity, 2),
            )

        if persist:
            open_risk = float(await trades.total_capital_at_risk())
            open_count = await trades.count_committed()
            await equity_repo.create(EquityCurve(
                equity=Decimal(str(round(equity, 2))),
                high_water_mark=Decimal(str(round(hwm, 2))),
                drawdown_pct=Decimal(str(round(drawdown, 4))),
                drawdown_state=state,
                realized_pnl=Decimal(str(round(realized, 2))),
                open_premium=Decimal(str(round(open_risk, 2))),
                open_positions=open_count,
            ))

        return DrawdownSnapshot(
            equity=equity,
            high_water_mark=hwm,
            drawdown_pct=drawdown,
            state=state,
            previous_state=previous_state,
        )
