from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.core.config import ConfigLoader
from src.database.models import DRAWDOWN_HALT, DRAWDOWN_NORMAL, EquityCurve
from src.database.repositories.equity import EquityRepository
from src.database.session import DatabaseManager

logger = structlog.get_logger("ResetHalt")


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    db = DatabaseManager.get_instance(config)

    async with db.session() as session:
        repo = EquityRepository(session)
        latest = await repo.get_latest()

        if latest is None:
            logger.info("no_equity_history", note="nothing to reset")
            return

        if latest.drawdown_state != DRAWDOWN_HALT:
            logger.info(
                "not_in_halt",
                current_state=latest.drawdown_state,
                note="reset applies only to the HALT state",
            )
            return

        await repo.create(EquityCurve(
            equity=latest.equity,
            high_water_mark=latest.equity,
            drawdown_pct=0,
            drawdown_state=DRAWDOWN_NORMAL,
            realized_pnl=latest.realized_pnl,
            open_premium=latest.open_premium,
            open_positions=latest.open_positions,
        ))
        logger.warning(
            "halt_reset",
            equity=float(latest.equity),
            note="high-water mark rebased to current equity — sizing restarts from NORMAL",
        )

    await db.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path))
