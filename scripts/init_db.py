from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import ConfigLoader
from src.database.session import DatabaseManager

logger = structlog.get_logger("InitDB")


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    db = DatabaseManager.get_instance(config)

    if not await db.health_check():
        raise RuntimeError(
            f"Cannot reach database '{config.database.name}' — "
            f"create it first: CREATE DATABASE {config.database.name};"
        )

    await db.create_tables()
    logger.info("init_complete", database=config.database.name)
    await db.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path))
