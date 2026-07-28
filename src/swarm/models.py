from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"


class SwarmOutcome(str, Enum):
    TRADE = "TRADE"
    VETO_REDTEAM = "VETO_REDTEAM"
    VETO_PRICEDIN = "VETO_PRICEDIN"
    VETO_LOW_CONVICTION = "VETO_LOW_CONVICTION"
    VETO_AGENT_ERROR = "VETO_AGENT_ERROR"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

    @property
    def is_veto(self) -> bool:
        return self.value.startswith("VETO")


class Thesis(BaseModel):
    stance: str = Field(description="BULL or BEAR — the side you were asked to argue")
    core_argument: str = Field(
        description="The single strongest reason the stock moves your way, in two sentences"
    )
    supporting_evidence: list[str] = Field(
        description="Two to five concrete facts drawn from the evidence provided. "
                    "Each must reference the source. Do not invent figures."
    )
    expected_move_pct: float = Field(
        description="Absolute size of the move you expect, in percent"
    )
    time_horizon_days: int = Field(description="Days until the move plays out")
    confidence: float = Field(description="0 to 1. How strong is this case, honestly")
    key_assumption: str = Field(
        description="The one assumption that, if false, destroys this thesis"
    )


class RedTeamVerdict(BaseModel):
    strongest_kill_argument: str = Field(
        description="The single strongest reason this trade loses money"
    )
    kill_confidence: float = Field(
        description="0 to 1. How confident are you that this argument holds. "
                    "Be low if you had to reach for it."
    )
    assumption_attacked: str = Field(
        description="Which stated assumption your argument breaks"
    )
    evidence: list[str] = Field(description="Concrete facts backing the attack")
    fatal: bool = Field(
        description="True only if this alone disqualifies the trade regardless of upside"
    )


class PricedInVerdict(BaseModel):
    priced_in_score: float = Field(
        description="0 = the market has not reacted at all, 1 = fully reflected in the price"
    )
    remaining_move_pct: float = Field(
        description="How much of the move is still available to capture, in percent"
    )
    crowding_risk: str = Field(description="LOW, MEDIUM or HIGH")
    reasoning: str = Field(description="Three sentences at most")


@dataclass
class AgentCost:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0

    def merge(self, other: "AgentCost") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.usd += other.usd


@dataclass
class SwarmVerdict:
    outcome: SwarmOutcome
    direction: Optional[str] = None
    conviction: float = 0.0
    expected_move_pct: float = 0.0
    time_horizon_days: int = 0
    veto_reason: Optional[str] = None
    bull: Optional[Thesis] = None
    bear: Optional[Thesis] = None
    redteam: Optional[RedTeamVerdict] = None
    pricedin: Optional[PricedInVerdict] = None
    cost: AgentCost = field(default_factory=AgentCost)
    warnings: list[str] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        return self.outcome is SwarmOutcome.TRADE

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "direction": self.direction,
            "conviction": round(self.conviction, 4),
            "expected_move_pct": round(self.expected_move_pct, 2),
            "time_horizon_days": self.time_horizon_days,
            "veto_reason": self.veto_reason,
            "warnings": self.warnings,
            "cost": {
                "calls": self.cost.calls,
                "input_tokens": self.cost.input_tokens,
                "output_tokens": self.cost.output_tokens,
                "usd": round(self.cost.usd, 6),
            },
        }


def compute_conviction(
    bull_confidence: float,
    bear_confidence: float,
    kill_confidence: float,
    priced_in_score: float,
) -> float:
    spread = abs(bull_confidence - bear_confidence)
    discounted = spread * (1.0 - kill_confidence) * (1.0 - priced_in_score)
    return max(0.0, min(1.0, discounted))


def synthesize(
    bull: Thesis,
    bear: Thesis,
    redteam: RedTeamVerdict,
    pricedin: PricedInVerdict,
    redteam_kill_threshold: float,
    pricedin_veto_threshold: float,
    min_conviction: float,
) -> SwarmVerdict:
    base = SwarmVerdict(
        outcome=SwarmOutcome.TRADE,
        bull=bull,
        bear=bear,
        redteam=redteam,
        pricedin=pricedin,
    )

    if redteam.fatal or redteam.kill_confidence > redteam_kill_threshold:
        base.outcome = SwarmOutcome.VETO_REDTEAM
        base.veto_reason = (
            f"red team kill_confidence {redteam.kill_confidence:.2f}"
            f"{' (fatal)' if redteam.fatal else ''}: {redteam.strongest_kill_argument}"
        )
        return base

    if pricedin.priced_in_score > pricedin_veto_threshold:
        base.outcome = SwarmOutcome.VETO_PRICEDIN
        base.veto_reason = (
            f"priced_in_score {pricedin.priced_in_score:.2f} — "
            f"only {pricedin.remaining_move_pct:.1f}% of the move is left"
        )
        return base

    if bull.confidence < min_conviction and bear.confidence < min_conviction:
        base.outcome = SwarmOutcome.VETO_LOW_CONVICTION
        base.veto_reason = (
            f"neither side reaches {min_conviction:.2f} "
            f"(bull {bull.confidence:.2f}, bear {bear.confidence:.2f})"
        )
        return base

    winner = bull if bull.confidence >= bear.confidence else bear
    base.direction = DIRECTION_LONG if winner is bull else DIRECTION_SHORT
    base.conviction = compute_conviction(
        bull.confidence, bear.confidence, redteam.kill_confidence, pricedin.priced_in_score
    )
    base.expected_move_pct = abs(pricedin.remaining_move_pct)
    base.time_horizon_days = winner.time_horizon_days
    return base
