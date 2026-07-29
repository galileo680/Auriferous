from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ExecutionOutcome(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    NO_FILL = "NO_FILL"
    ERROR = "ERROR"

    @property
    def opened_a_position(self) -> bool:
        return self in (ExecutionOutcome.FILLED, ExecutionOutcome.PARTIAL)


@dataclass
class ExecutionResult:
    outcome: ExecutionOutcome
    order_id: Optional[str] = None
    requested_quantity: int = 0
    filled_quantity: int = 0
    avg_price: float = 0.0
    commission: float = 0.0
    price_moves: int = 0
    final_limit: float = 0.0
    reason: str = ""
