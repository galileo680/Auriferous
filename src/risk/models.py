from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.database.models import DRAWDOWN_NORMAL

P_SOURCE_CALIBRATED = "CALIBRATED"
P_SOURCE_FALLBACK = "FALLBACK"


@dataclass
class RiskVerdict:
    approved: bool
    quantity: int = 0
    capital_at_risk: float = 0.0
    kelly_fraction_used: float = 0.0
    drawdown_state: str = DRAWDOWN_NORMAL
    veto_reason: Optional[str] = None
    hit_rate_used: float = 0.0
    payoff_odds_used: float = 0.0
    hit_rate_source: str = P_SOURCE_FALLBACK

    def to_payload(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "quantity": self.quantity,
            "capital_at_risk": round(self.capital_at_risk, 2),
            "kelly_fraction_used": round(self.kelly_fraction_used, 4),
            "drawdown_state": self.drawdown_state,
            "veto_reason": self.veto_reason,
            "hit_rate_used": round(self.hit_rate_used, 4),
            "payoff_odds_used": round(self.payoff_odds_used, 4),
            "hit_rate_source": self.hit_rate_source,
        }
