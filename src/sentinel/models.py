from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.core.clock import utcnow_naive

MARKET_EQUITY = "EQUITY"
MARKET_CRYPTO = "CRYPTO"

SOURCE_EDGAR_8K = "EDGAR_8K"
SOURCE_EDGAR_DILUTION = "EDGAR_DILUTION"
SOURCE_EDGAR_13D = "EDGAR_13D"
SOURCE_HALT = "HALT"
SOURCE_PDUFA = "PDUFA"
SOURCE_VOLUME_ANOMALY = "VOLUME_ANOMALY"
SOURCE_EARNINGS_CALENDAR = "EARNINGS_CALENDAR"
SOURCE_CRYPTO_FUNDING = "CRYPTO_FUNDING"
SOURCE_CRYPTO_OI = "CRYPTO_OI"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_UNCLEAR = "UNCLEAR"

PRIORITY_CRITICAL = 1
PRIORITY_HIGH = 2
PRIORITY_NORMAL = 3
PRIORITY_LOW = 4
PRIORITY_BACKGROUND = 5

EDGAR_ITEM_RULES: dict[str, tuple[int, str, str]] = {
    "1.01": (PRIORITY_HIGH, DIRECTION_LONG, "material definitive agreement"),
    "1.03": (PRIORITY_CRITICAL, DIRECTION_SHORT, "bankruptcy or receivership"),
    "2.02": (PRIORITY_HIGH, DIRECTION_UNCLEAR, "results of operations"),
    "3.02": (PRIORITY_HIGH, DIRECTION_SHORT, "unregistered equity sale"),
    "4.01": (PRIORITY_HIGH, DIRECTION_SHORT, "auditor change"),
    "4.02": (PRIORITY_CRITICAL, DIRECTION_SHORT, "prior statements not reliable"),
    "5.02": (PRIORITY_NORMAL, DIRECTION_UNCLEAR, "officer or director departure"),
    "7.01": (PRIORITY_NORMAL, DIRECTION_UNCLEAR, "regulation FD disclosure"),
    "8.01": (PRIORITY_NORMAL, DIRECTION_UNCLEAR, "other events"),
}

HALT_CODE_RULES: dict[str, tuple[int, str]] = {
    "T1": (PRIORITY_CRITICAL, "news pending"),
    "T12": (PRIORITY_CRITICAL, "additional information requested"),
    "LUDP": (PRIORITY_HIGH, "limit up / limit down volatility pause"),
}

DILUTION_FORMS = ("S-1", "S-3", "424B5", "424B3", "S-1/A", "S-3/A")


@dataclass
class RawEvent:
    source: str
    ticker: str
    market: str
    dedup_key: str
    priority: int = PRIORITY_NORMAL
    direction: str = DIRECTION_UNCLEAR
    payload: dict[str, Any] = field(default_factory=dict)
    raw_text: Optional[str] = None
    detected_at: datetime = field(default_factory=utcnow_naive)

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()

    def describe(self) -> str:
        return f"{self.source}:{self.ticker}:p{self.priority}"


class EventSource(ABC):

    name: str = "unnamed"
    market: str = MARKET_EQUITY

    @abstractmethod
    async def poll(self) -> list[RawEvent]:
        ...

    async def close(self) -> None:
        return None


def classify_edgar_items(items: list[str]) -> tuple[int, str, list[str]]:
    known = [item for item in items if item in EDGAR_ITEM_RULES]
    if not known:
        return PRIORITY_BACKGROUND, DIRECTION_UNCLEAR, []

    ranked = sorted(known, key=lambda item: EDGAR_ITEM_RULES[item][0])
    priority = EDGAR_ITEM_RULES[ranked[0]][0]

    directions = {EDGAR_ITEM_RULES[item][1] for item in known}
    directions.discard(DIRECTION_UNCLEAR)

    if len(directions) == 1:
        direction = directions.pop()
    else:
        direction = DIRECTION_UNCLEAR

    return priority, direction, ranked


def classify_halt(reason_code: str) -> tuple[int, str] | None:
    return HALT_CODE_RULES.get(reason_code.upper())
