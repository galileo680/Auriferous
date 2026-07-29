from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog
from sqlalchemy import inspect, text

from src.core.config import ConfigLoader
from src.database.models import Base
from src.database.session import DatabaseManager

logger = structlog.get_logger("MigrateSchema")


def _missing_columns(connection) -> list[tuple[str, str, str]]:
    inspector = inspect(connection)
    dialect = connection.dialect
    missing: list[tuple[str, str, str]] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            missing.append((
                table.name,
                column.name,
                column.type.compile(dialect=dialect),
            ))
    return missing


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    db = DatabaseManager.get_instance(config)

    if not await db.health_check():
        raise RuntimeError(f"Cannot reach database '{config.database.name}'")

    await db.create_tables()

    async with db.engine().begin() as conn:
        missing = await conn.run_sync(_missing_columns)
        for table_name, column_name, column_type in missing:
            await conn.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            ))
            logger.info(
                "column_added",
                table=table_name,
                column=column_name,
                type=column_type,
            )

    if not missing:
        logger.info("schema_up_to_date", database=config.database.name)
    else:
        logger.info("migration_complete", columns_added=len(missing))

    await db.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path))
