from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.core.market_clock import MarketState

QUEUE_MAX_AGE_HOURS = 18


class GateDecision(str, Enum):
    ANALYZE_NOW = "ANALYZE_NOW"
    QUEUE_FOR_OPEN = "QUEUE_FOR_OPEN"
    EXPIRE = "EXPIRE"


@dataclass(frozen=True)
class GateVerdict:
    decision: GateDecision
    reason: str


def decide(
    state: MarketState,
    age_hours: float,
    already_queued: bool = False,
) -> GateVerdict:
    if age_hours > QUEUE_MAX_AGE_HOURS:
        return GateVerdict(
            GateDecision.EXPIRE,
            f"event is {age_hours:.1f}h old — catalyst is priced in by now",
        )

    if state is MarketState.CLOSED:
        return GateVerdict(
            GateDecision.QUEUE_FOR_OPEN,
            "market closed — analysis deferred to avoid stale quotes",
        )

    if state is MarketState.REGULAR:
        return GateVerdict(GateDecision.ANALYZE_NOW, "regular session — full pipeline")

    if already_queued:
        return GateVerdict(
            GateDecision.QUEUE_FOR_OPEN,
            "already analysed — waiting for the opening bell to place the order",
        )

    return GateVerdict(
        GateDecision.ANALYZE_NOW,
        f"{state.value} — analyse now so the decision is ready at the open",
    )
