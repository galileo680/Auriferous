from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.core.config import PositionsConfig

EXIT_HARD_EXPIRY = "HARD_EXPIRY"
EXIT_CONTRARY_EVENT = "CONTRARY_EVENT"
EXIT_HORIZON = "HORIZON_ELAPSED"
EXIT_THETA = "THETA_EXIT"
EXIT_PREMIUM_STOP = "PREMIUM_STOP"
EXIT_STOCK_STOP = "STOCK_STOP"
EXIT_SCALE_OUT = "SCALE_OUT"

HARD_EXPIRY_TRADING_DAYS = 2
THRESHOLD_EPSILON = 1e-9

ERROR_EXPIRY_CLOSE_FAILED = "EXPIRY_CLOSE_FAILED"
ERROR_RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
BLOCKING_ERROR_TYPES = (ERROR_EXPIRY_CLOSE_FAILED, ERROR_RECONCILE_MISMATCH)


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    fraction: float

    @property
    def is_full(self) -> bool:
        return self.fraction >= 1.0


def exit_decision(
    *,
    is_option: bool,
    is_stock: bool,
    value: Optional[float],
    entry: float,
    days_to_expiry: Optional[int],
    trading_days_to_expiry: Optional[int],
    horizon_elapsed: bool,
    contrary_event: bool,
    scaled_out: bool,
    stop_fraction: Optional[float],
    config: PositionsConfig,
) -> Optional[ExitDecision]:
    if (
        is_option
        and trading_days_to_expiry is not None
        and trading_days_to_expiry <= HARD_EXPIRY_TRADING_DAYS
    ):
        return ExitDecision(EXIT_HARD_EXPIRY, 1.0)

    if contrary_event:
        return ExitDecision(EXIT_CONTRARY_EVENT, 1.0)

    if horizon_elapsed:
        return ExitDecision(EXIT_HORIZON, 1.0)

    if (
        is_option
        and days_to_expiry is not None
        and days_to_expiry <= config.theta_exit_days
    ):
        return ExitDecision(EXIT_THETA, 1.0)

    if value is not None and entry > 0:
        change = value / entry - 1.0

        if is_option and change <= config.option_stop_premium_pct + THRESHOLD_EPSILON:
            return ExitDecision(EXIT_PREMIUM_STOP, 1.0)

        if is_stock and stop_fraction and change <= -stop_fraction + THRESHOLD_EPSILON:
            return ExitDecision(EXIT_STOCK_STOP, 1.0)

        if (
            is_option
            and not scaled_out
            and change >= config.scale_out_at_gain_pct - THRESHOLD_EPSILON
        ):
            return ExitDecision(EXIT_SCALE_OUT, config.scale_out_fraction)

    return None


@dataclass
class ManagerRunResult:
    examined: int = 0
    closed: int = 0
    scaled: int = 0
    failed_exits: int = 0
    unrealized_pnl: float = 0.0
    market_closed: bool = False
    errors: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)


@dataclass
class ReconcileRunResult:
    checked_con_ids: int = 0
    mismatches: int = 0
    alerted: bool = False
