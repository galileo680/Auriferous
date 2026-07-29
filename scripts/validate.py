from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import AuriferousConfig, ConfigLoader
from src.database.models import (
    DRAWDOWN_HALT,
    TRADE_STATUS_CLOSED,
    Analysis,
    ShadowTrade,
    Trade,
)
from src.database.repositories import EquityRepository, ErrorRepository
from src.database.session import DatabaseManager
from src.positions.models import BLOCKING_ERROR_TYPES
from src.shadow.book import BOOK_SHADOW, ORIGIN_REDTEAM_VETO
from src.shadow.metrics import priced_in_calibration, triage_precision, veto_value

MIN_DECISIONS = 60
MIN_TRIAGE_PRECISION = 0.25
MAX_PRICEDIN_CORRELATION = -0.25
KILL_DRAWDOWN = 0.40
KILL_MIN_DECISIONS = 100
KILL_LLM_COST_PCT = 0.15


@dataclass
class Criterion:
    name: str
    passed: Optional[bool]
    detail: str


async def _closed_counts(session: AsyncSession) -> tuple[int, int]:
    shadow = (await session.execute(
        select(func.count(ShadowTrade.id)).where(
            ShadowTrade.book == BOOK_SHADOW,
            ShadowTrade.status == "CLOSED",
        )
    )).scalar() or 0
    real = (await session.execute(
        select(func.count(Trade.id)).where(Trade.status == TRADE_STATUS_CLOSED)
    )).scalar() or 0
    return real, shadow


async def _mean_pnl_pct(session: AsyncSession) -> tuple[Optional[float], int]:
    returns: list[float] = []

    shadows = (await session.execute(
        select(ShadowTrade).where(
            ShadowTrade.book == BOOK_SHADOW,
            ShadowTrade.status == "CLOSED",
            ShadowTrade.pnl_virtual.isnot(None),
        )
    )).scalars().all()
    for row in shadows:
        basis = float(row.entry_price) * abs(row.quantity)
        if basis > 0:
            returns.append(float(row.pnl_virtual) / basis)

    trades = (await session.execute(
        select(Trade).where(
            Trade.status == TRADE_STATUS_CLOSED,
            Trade.pnl_realized.isnot(None),
        )
    )).scalars().all()
    for trade in trades:
        basis = float(trade.capital_at_risk or 0)
        if basis > 0:
            returns.append(float(trade.pnl_realized) / basis)

    if not returns:
        return None, 0
    return sum(returns) / len(returns), len(returns)


async def evaluate(session: AsyncSession, config: AuriferousConfig) -> list[Criterion]:
    criteria: list[Criterion] = []

    real, shadow = await _closed_counts(session)
    total = real + shadow
    criteria.append(Criterion(
        f"closed decisions >= {MIN_DECISIONS}",
        total >= MIN_DECISIONS,
        f"{total} (real {real}, virtual {shadow})",
    ))

    blocking = await ErrorRepository(session).unresolved(BLOCKING_ERROR_TYPES)
    criteria.append(Criterion(
        "no unresolved critical errors",
        len(blocking) == 0,
        ", ".join(e.error_type for e in blocking) or "clean",
    ))

    halt_entries = (await session.execute(
        select(func.count(Trade.id)).where(
            Trade.drawdown_state_at_entry == DRAWDOWN_HALT
        )
    )).scalar() or 0
    criteria.append(Criterion(
        "no entries while in HALT",
        halt_entries == 0,
        f"{halt_entries} violations",
    ))

    precision = await triage_precision(session, config.triage.min_expected_move_pct)
    criteria.append(Criterion(
        f"triage precision >= {MIN_TRIAGE_PRECISION:.0%}",
        None if precision is None else precision >= MIN_TRIAGE_PRECISION,
        "no data" if precision is None else f"{precision:.1%}",
    ))

    values = await veto_value(session)
    redteam = values.get(ORIGIN_REDTEAM_VETO)
    criteria.append(Criterion(
        "veto value REDTEAM <= 0",
        None if redteam is None else redteam <= 0,
        "no closed redteam vetoes" if redteam is None else f"${redteam:,.2f}",
    ))

    correlation = await priced_in_calibration(session)
    criteria.append(Criterion(
        f"priced-in correlation <= {MAX_PRICEDIN_CORRELATION}",
        None if correlation is None else correlation <= MAX_PRICEDIN_CORRELATION,
        "insufficient samples" if correlation is None else f"r = {correlation:+.3f}",
    ))

    return criteria


async def kill_criteria(
    session: AsyncSession,
    config: AuriferousConfig,
) -> list[Criterion]:
    triggered: list[Criterion] = []

    latest = await EquityRepository(session).get_latest()
    drawdown = float(latest.drawdown_pct) if latest else 0.0
    triggered.append(Criterion(
        f"drawdown > {KILL_DRAWDOWN:.0%}",
        drawdown > KILL_DRAWDOWN,
        f"{drawdown:.1%}",
    ))

    mean_pnl, n = await _mean_pnl_pct(session)
    triggered.append(Criterion(
        f"edge < 0 after >= {KILL_MIN_DECISIONS} decisions",
        mean_pnl is not None and n >= KILL_MIN_DECISIONS and mean_pnl < 0,
        f"mean {mean_pnl:+.4f} over {n}" if mean_pnl is not None else "no data",
    ))

    values = await veto_value(session)
    triggered.append(Criterion(
        "veto value positive for every origin",
        len(values) >= 3 and all(v > 0 for v in values.values()),
        f"{len(values)} origins" if values else "no data",
    ))

    first, last = (await session.execute(
        select(func.min(Analysis.created_at), func.max(Analysis.created_at))
    )).one()
    span_days = (last - first).total_seconds() / 86400 if first and last else 0.0
    total_cost = float((await session.execute(
        select(func.coalesce(func.sum(Analysis.llm_cost_usd), 0))
    )).scalar() or 0)
    annualized = total_cost / max(span_days, 1.0) * 365
    limit = KILL_LLM_COST_PCT * config.capital.initial_usd
    triggered.append(Criterion(
        f"LLM cost > {KILL_LLM_COST_PCT:.0%} of capital per year",
        annualized > limit,
        f"${annualized:,.0f}/yr vs ${limit:,.0f} limit",
    ))

    return triggered


def _symbol(passed: Optional[bool], invert: bool = False) -> str:
    if passed is None:
        return "?"
    ok = not passed if invert else passed
    return "PASS" if ok else "FAIL"


async def main(config_path: str = "config/auriferous.yaml") -> int:
    config = ConfigLoader.load(config_path=config_path)
    db = DatabaseManager.get_instance(config)

    async with db.session() as session:
        criteria = await evaluate(session, config)
        kills = await kill_criteria(session, config)

    print("\nSTAGE 1 VALIDATION (§14.1)\n" + "-" * 68)
    for c in criteria:
        print(f"[{_symbol(c.passed):>4}] {c.name:<42} {c.detail}")

    print("\nKILL CRITERIA (§14.3) — triggered = stop and review\n" + "-" * 68)
    for c in kills:
        label = "KILL" if c.passed else "ok"
        print(f"[{label:>4}] {c.name:<42} {c.detail}")

    await db.close()

    if any(c.passed for c in kills):
        print("\nVERDICT: KILL CRITERION TRIGGERED — stop and review the strategy")
        return 1
    if any(c.passed is False for c in criteria):
        print("\nVERDICT: NOT READY — stage 1 criteria failing")
        return 1
    if any(c.passed is None for c in criteria):
        print("\nVERDICT: INSUFFICIENT DATA — keep the paper validation running")
        return 2
    print("\nVERDICT: STAGE 1 PASSED — eligible for stage 2 ($500 real capital)")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    sys.exit(asyncio.run(main(path)))
