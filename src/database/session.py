from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import AuriferousConfig, ConfigLoader
from src.database.models import Base


class DatabaseManager:
    _instance: Optional["DatabaseManager"] = None

    def __init__(self, config: AuriferousConfig) -> None:
        self._config = config
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._logger = structlog.get_logger("DatabaseManager")

    @classmethod
    def get_instance(cls, config: AuriferousConfig | None = None) -> "DatabaseManager":
        if cls._instance is None:
            cls._instance = cls(config or ConfigLoader.get())
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._config.database.url(),
                echo=False,
                pool_pre_ping=True,
            )
            self._session_factory = async_sessionmaker(
                self._engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        self.engine()
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_tables(self) -> None:
        async with self.engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._logger.info("tables_created", database=self._config.database.name)

    async def health_check(self) -> bool:
        try:
            async with self.engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            self._logger.error("database_health_check_failed", error=str(e))
            return False

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
