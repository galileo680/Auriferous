from __future__ import annotations

import structlog

from src.database.session import DatabaseManager
from src.shadow.book import ShadowBookService, ShadowSyncResult
from src.shadow.calibrator import Calibrator


class ShadowSyncLoop:

    def __init__(self, service: ShadowBookService) -> None:
        self._service = service
        self._logger = structlog.get_logger("ShadowSyncLoop")

    async def run(self) -> ShadowSyncResult:
        db = DatabaseManager.get_instance()
        async with db.session() as session:
            return await self._service.sync(session)


class CalibratorLoop:

    def __init__(self, calibrator: Calibrator) -> None:
        self._calibrator = calibrator
        self._logger = structlog.get_logger("CalibratorLoop")

    async def run(self) -> int:
        db = DatabaseManager.get_instance()
        async with db.session() as session:
            written = await self._calibrator.run(session)
        if written:
            self._logger.info("calibration_written", buckets=written)
        return written
