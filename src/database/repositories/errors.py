from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import ErrorLog

from .base import BaseRepository


class ErrorRepository(BaseRepository[ErrorLog]):

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, ErrorLog)

    async def unresolved(self, error_types: Iterable[str]) -> list[ErrorLog]:
        result = await self.session.execute(
            select(self.model).where(
                self.model.resolved.is_(False),
                self.model.error_type.in_(list(error_types)),
            )
        )
        return list(result.scalars().all())

    async def record_once(
        self,
        component: str,
        error_type: str,
        message: str,
        context: Optional[dict] = None,
    ) -> bool:
        existing = await self.unresolved([error_type])
        if existing:
            return False

        await self.create(ErrorLog(
            component=component,
            error_type=error_type,
            message=message,
            context=context,
        ))
        return True

    async def resolve_all(self, error_types: Iterable[str]) -> int:
        rows = await self.unresolved(error_types)
        for row in rows:
            row.resolved = True
        await self.session.flush()
        return len(rows)
