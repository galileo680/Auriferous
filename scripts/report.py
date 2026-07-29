from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from src.core.config import ConfigLoader
from src.database.models import (
    TRADE_STATUS_CLOSED,
    TRADE_STATUS_OPEN,
    Analysis,
    CalibrationSnapshot,
    ShadowTrade,
    Trade,
)
from src.database.repositories import EquityRepository
from src.database.session import DatabaseManager
from src.shadow.metrics import (
    manager_value,
    priced_in_calibration,
    triage_precision,
    veto_value,
)

LINE = "-" * 68


def header(title: str) -> None:
    print(f"\n{title}\n{LINE}")


async def main(config_path: str = "config/auriferous.yaml") -> None:
    config = ConfigLoader.load(config_path=config_path)
    db = DatabaseManager.get_instance(config)

    async with db.session() as session:
        header("EQUITY")
        latest = await EquityRepository(session).get_latest()
        if latest is None:
            print("no equity history yet")
        else:
            print(f"equity            ${float(latest.equity):>12,.2f}")
            print(f"high-water mark   ${float(latest.high_water_mark):>12,.2f}")
            print(f"drawdown          {float(latest.drawdown_pct):>12.2%}")
            print(f"state             {latest.drawdown_state:>13}")
            print(f"realized pnl      ${float(latest.realized_pnl):>12,.2f}")

        header("TRADES")
        open_count = (await session.execute(
            select(func.count(Trade.id)).where(Trade.status == TRADE_STATUS_OPEN)
        )).scalar() or 0
        closed = (await session.execute(
            select(Trade).where(Trade.status == TRADE_STATUS_CLOSED)
        )).scalars().all()
        pnl_total = sum(float(t.pnl_realized or 0) for t in closed)
        commissions = (await session.execute(
            select(func.coalesce(func.sum(Trade.commission_total), 0))
        )).scalar() or 0
        wins = sum(1 for t in closed if float(t.pnl_realized or 0) > 0)
        print(f"open positions    {open_count:>13}")
        print(f"closed trades     {len(closed):>13}")
        if closed:
            print(f"hit rate          {wins / len(closed):>13.1%}")
        print(f"realized pnl      ${pnl_total:>12,.2f}")
        print(f"commissions paid  ${float(commissions):>12,.2f}")

        header("LLM COST")
        llm_cost = (await session.execute(
            select(func.coalesce(func.sum(Analysis.llm_cost_usd), 0))
        )).scalar() or 0
        analyses_count = (await session.execute(
            select(func.count(Analysis.id))
        )).scalar() or 0
        print(f"analyses          {analyses_count:>13}")
        print(f"swarm cost        ${float(llm_cost):>12,.2f}")

        header("CALIBRATION (latest snapshot per bucket)")
        latest_per_bucket = (await session.execute(
            select(CalibrationSnapshot)
            .order_by(CalibrationSnapshot.as_of.desc())
            .limit(200)
        )).scalars().all()
        seen: set[tuple[str, str]] = set()
        printed = False
        for row in latest_per_bucket:
            key = (row.catalyst_type, row.conviction_bucket)
            if key in seen:
                continue
            seen.add(key)
            printed = True
            print(
                f"{row.catalyst_type:<24} {row.conviction_bucket:<8} "
                f"n={row.sample_size:<4} hit={float(row.hit_rate or 0):.2f} "
                f"win={float(row.avg_win_pct or 0):+.1f}% "
                f"loss={float(row.avg_loss_pct or 0):+.1f}% "
                f"edge={float(row.edge or 0):+.4f}"
            )
        if not printed:
            print("no calibration data yet — the fallback hit rate 0.35 is in force")

        header("SHADOW BOOK — veto value per origin")
        open_shadow = (await session.execute(
            select(ShadowTrade.book, func.count(ShadowTrade.id))
            .where(ShadowTrade.status == "OPEN")
            .group_by(ShadowTrade.book)
        )).all()
        for book, count in open_shadow:
            print(f"open virtual ({book.lower():<9}) {count:>6}")
        values = await veto_value(session)
        if not values:
            print("no closed virtual positions yet")
        for origin, total in sorted(values.items(), key=lambda kv: kv[1]):
            note = "  <- this filter rejects profitable trades" if total > 0 else ""
            print(f"{origin:<20} ${total:>12,.2f}{note}")

        header("PIPELINE QUALITY")
        mv = await manager_value(session)
        print(
            f"manager value     "
            f"{'n/a (no closed pairs)' if mv is None else f'${mv:,.2f}'}"
        )
        tp = await triage_precision(session, config.triage.min_expected_move_pct)
        print(
            f"triage precision  "
            f"{'n/a' if tp is None else f'{tp:.1%}'}"
            f"  (target >= 25%)"
        )
        pic = await priced_in_calibration(session)
        print(
            f"priced-in corr    "
            f"{'n/a (needs >= 3 samples)' if pic is None else f'{pic:+.3f}'}"
            f"  (target negative, |r| >= 0.25)"
        )

    await db.close()
    print()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    asyncio.run(main(path))
