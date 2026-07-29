from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import ConfigLoader
from src.database.repositories import ErrorRepository
from src.database.session import DatabaseManager
from src.positions.models import BLOCKING_ERROR_TYPES

logger = structlog.get_logger("ResolveErrors")


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    db = DatabaseManager.get_instance(config)

    async with db.session() as session:
        repo = ErrorRepository(session)
        pending = await repo.unresolved(BLOCKING_ERROR_TYPES)

        if not pending:
            logger.info("no_blocking_errors")
        else:
            for row in pending:
                logger.warning(
                    "resolving_error",
                    error_type=row.error_type,
                    message=row.message,
                    created_at=str(row.created_at),
                )
            count = await repo.resolve_all(BLOCKING_ERROR_TYPES)
            logger.warning(
                "errors_resolved",
                count=count,
                note="verify positions at the broker before restarting the system",
            )

    await db.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path))
