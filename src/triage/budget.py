from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import TriageConfig
from src.database.repositories import EventRepository


@dataclass
class BudgetStatus:
    allowed: int
    used_today: int
    used_this_hour: int
    daily_limit: int
    hourly_limit: int

    @property
    def exhausted(self) -> bool:
        return self.allowed <= 0

    def reason(self) -> str:
        if self.used_today >= self.daily_limit:
            return f"daily triage budget spent ({self.used_today}/{self.daily_limit})"
        if self.used_this_hour >= self.hourly_limit:
            return f"hourly triage budget spent ({self.used_this_hour}/{self.hourly_limit})"
        return "budget available"


class TriageBudget:

    def __init__(self, config: TriageConfig) -> None:
        self._config = config
        self._logger = structlog.get_logger("TriageBudget")

    async def status(self, session: AsyncSession) -> BudgetStatus:
        repo = EventRepository(session)

        used_today = await repo.count_triaged_since(hours=24)
        used_this_hour = await repo.count_triaged_since(hours=1)

        remaining_day = self._config.max_per_day - used_today
        remaining_hour = self._config.max_per_hour - used_this_hour

        return BudgetStatus(
            allowed=max(min(remaining_day, remaining_hour), 0),
            used_today=used_today,
            used_this_hour=used_this_hour,
            daily_limit=self._config.max_per_day,
            hourly_limit=self._config.max_per_hour,
        )
