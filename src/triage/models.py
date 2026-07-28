from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

CATALYST_EARNINGS_SURPRISE = "EARNINGS_SURPRISE"
CATALYST_FDA_DECISION = "FDA_DECISION"
CATALYST_MA = "MA"
CATALYST_DILUTION = "DILUTION"
CATALYST_GUIDANCE = "GUIDANCE"
CATALYST_LEGAL = "LEGAL"
CATALYST_MGMT_CHANGE = "MGMT_CHANGE"
CATALYST_ACCOUNTING_RED_FLAG = "ACCOUNTING_RED_FLAG"
CATALYST_CRYPTO_FLOW = "CRYPTO_FLOW"
CATALYST_NOISE = "NOISE"

CATALYST_TYPES = (
    CATALYST_EARNINGS_SURPRISE,
    CATALYST_FDA_DECISION,
    CATALYST_MA,
    CATALYST_DILUTION,
    CATALYST_GUIDANCE,
    CATALYST_LEGAL,
    CATALYST_MGMT_CHANGE,
    CATALYST_ACCOUNTING_RED_FLAG,
    CATALYST_CRYPTO_FLOW,
    CATALYST_NOISE,
)


class TriageOutcome(str, Enum):
    PROMOTED = "PROMOTED"
    REJECTED_NOT_ACTIONABLE = "REJECTED_NOT_ACTIONABLE"
    REJECTED_UNCLEAR_DIRECTION = "REJECTED_UNCLEAR_DIRECTION"
    REJECTED_MOVE_TOO_SMALL = "REJECTED_MOVE_TOO_SMALL"
    REJECTED_LLM_ERROR = "REJECTED_LLM_ERROR"
    QUEUED = "QUEUED"
    EXPIRED = "EXPIRED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

    @property
    def is_rejection(self) -> bool:
        return self.value.startswith("REJECTED")


class TriageResult(BaseModel):
    is_actionable: bool = Field(
        description="True only if this event could plausibly move the stock by the "
                    "required threshold within days. Default to False."
    )
    catalyst_type: str = Field(
        description="One of: EARNINGS_SURPRISE, FDA_DECISION, MA, DILUTION, GUIDANCE, "
                    "LEGAL, MGMT_CHANGE, ACCOUNTING_RED_FLAG, CRYPTO_FLOW, NOISE"
    )
    direction: str = Field(
        description="LONG if the stock should rise, SHORT if it should fall, "
                    "UNCLEAR if the sign genuinely cannot be determined"
    )
    expected_move_pct: float = Field(
        description="Absolute expected move in percent, ignoring sign. Be honest: "
                    "most events move a stock less than 3%."
    )
    time_to_impact_hours: int = Field(
        description="Hours until the market has fully priced this in"
    )
    reasoning: str = Field(
        description="At most three sentences. State the mechanism, not a summary."
    )


@dataclass
class MarketContext:
    ticker: str
    last_price: Optional[float] = None
    change_1d_pct: Optional[float] = None
    change_5d_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    after_hours_move_pct: Optional[float] = None
    market_cap: Optional[float] = None
    sector: Optional[str] = None
    has_options: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "last_price": self.last_price,
            "change_1d_pct": self.change_1d_pct,
            "change_5d_pct": self.change_5d_pct,
            "volume_ratio": self.volume_ratio,
            "after_hours_move_pct": self.after_hours_move_pct,
            "market_cap": self.market_cap,
            "sector": self.sector,
            "has_options": self.has_options,
            "warnings": self.warnings,
        }

    def render(self) -> str:
        def fmt(value: Optional[float], suffix: str = "", digits: int = 2) -> str:
            return f"{value:.{digits}f}{suffix}" if value is not None else "unavailable"

        cap = (
            f"${self.market_cap / 1_000_000:.0f}M"
            if self.market_cap
            else "unavailable"
        )

        lines = [
            f"- Last price: {fmt(self.last_price)}",
            f"- Move today: {fmt(self.change_1d_pct, '%')}",
            f"- Move over 5 sessions: {fmt(self.change_5d_pct, '%')}",
            f"- Volume vs 20-day average: {fmt(self.volume_ratio, 'x')}",
            f"- Move in extended hours: {fmt(self.after_hours_move_pct, '%')}",
            f"- Market cap: {cap}",
            f"- Sector: {self.sector or 'unknown'}",
            f"- Options available: {'yes' if self.has_options else 'unknown'}",
        ]
        if self.warnings:
            lines.append(f"- Data warnings: {'; '.join(self.warnings)}")
        return "\n".join(lines)


@dataclass
class TriageDecision:
    outcome: TriageOutcome
    result: Optional[TriageResult] = None
    context: Optional[MarketContext] = None
    reason: str = ""
    cost_usd: float = 0.0

    @property
    def promoted(self) -> bool:
        return self.outcome is TriageOutcome.PROMOTED

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "cost_usd": round(self.cost_usd, 6),
        }
        if self.result is not None:
            payload["result"] = self.result.model_dump()
        if self.context is not None:
            payload["context"] = self.context.to_dict()
        return payload


def evaluate_gate(
    result: TriageResult,
    min_expected_move_pct: float,
) -> tuple[TriageOutcome, str]:
    if not result.is_actionable:
        return (
            TriageOutcome.REJECTED_NOT_ACTIONABLE,
            "triage judged the event not actionable",
        )

    if result.direction.upper() not in ("LONG", "SHORT"):
        return (
            TriageOutcome.REJECTED_UNCLEAR_DIRECTION,
            f"direction is {result.direction} — no tradeable side",
        )

    if abs(result.expected_move_pct) < min_expected_move_pct:
        return (
            TriageOutcome.REJECTED_MOVE_TOO_SMALL,
            f"expected move {result.expected_move_pct:.1f}% "
            f"below the {min_expected_move_pct:.1f}% threshold",
        )

    return TriageOutcome.PROMOTED, "passed all triage gates"


def normalize_catalyst_type(value: str) -> str:
    cleaned = (value or "").strip().upper().replace(" ", "_")
    return cleaned if cleaned in CATALYST_TYPES else CATALYST_NOISE
