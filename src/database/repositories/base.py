from __future__ import annotations

from typing import Generic, Optional, Type, TypeVar

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class BaseRepository(Generic[T]):

    def __init__(self, session: AsyncSession, model: Type[T]) -> None:
        self.session = session
        self.model = model
        self.logger = structlog.get_logger(self.__class__.__name__)

    async def get_by_id(self, entity_id: int) -> Optional[T]:
        result = await self.session.execute(
            select(self.model).where(self.model.id == entity_id)
        )
        return result.scalar_one_or_none()

    async def create(self, entity: T) -> T:
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def create_many(self, entities: list[T]) -> list[T]:
        self.session.add_all(entities)
        await self.session.flush()
        return entities

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(self.model.id)))
        return result.scalar() or 0
