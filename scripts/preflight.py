from __future__ import annotations

import asyncio
import csv
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from src.broker.ibkr import IBKRClient
from src.core.config import ConfigLoader
from src.database.models import DRAWDOWN_HALT, Event
from src.database.repositories import EquityRepository, ErrorRepository
from src.database.session import DatabaseManager
from src.positions.models import BLOCKING_ERROR_TYPES
from src.sentinel.universe import UNIVERSE_PATH, load_universe

PDUFA_PATH = Path("data/pdufa_calendar.csv")
MIN_UNIVERSE_SIZE = 500
MAX_UNIVERSE_AGE_DAYS = 14

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


class Checklist:

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))

    @property
    def ok(self) -> bool:
        return all(status != FAIL for status, _, _ in self.rows)

    def render(self) -> None:
        print("\nPREFLIGHT CHECK\n" + "-" * 68)
        for status, name, detail in self.rows:
            print(f"[{status:>4}] {name:<40} {detail}")
        print(
            "\nVERDICT: "
            + ("READY" if self.ok else "NOT READY — fix the FAIL items first")
        )


def check_universe(checklist: Checklist) -> None:
    path = Path(UNIVERSE_PATH)
    if not path.exists():
        checklist.add(FAIL, "universe file", "missing — run scripts/refresh_universe.py")
        return

    entries = load_universe()
    age_days = (
        datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    ).days
    if len(entries) < MIN_UNIVERSE_SIZE:
        checklist.add(FAIL, "universe size", f"{len(entries)} < {MIN_UNIVERSE_SIZE}")
    else:
        checklist.add(PASS, "universe size", f"{len(entries)} tickers")

    if age_days > MAX_UNIVERSE_AGE_DAYS:
        checklist.add(WARN, "universe freshness", f"{age_days}d old — refresh it")
    else:
        checklist.add(PASS, "universe freshness", f"{age_days}d old")

    with_sector = sum(1 for e in entries if e.sector)
    if entries and with_sector / len(entries) < 0.5:
        checklist.add(WARN, "sector coverage", f"{with_sector}/{len(entries)}")
    else:
        checklist.add(PASS, "sector coverage", f"{with_sector}/{len(entries)}")


def check_pdufa(checklist: Checklist) -> None:
    if not PDUFA_PATH.exists():
        checklist.add(FAIL, "pdufa calendar", "missing — run scripts/refresh_pdufa.py")
        return
    try:
        with open(PDUFA_PATH, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        future = sum(
            1 for row in rows
            if date.fromisoformat(row["pdufa_date"]) >= date.today()
        )
    except Exception as e:
        checklist.add(WARN, "pdufa calendar", f"unreadable: {e}")
        return

    if future == 0:
        checklist.add(WARN, "pdufa calendar", "no future dates — refresh it")
    else:
        checklist.add(PASS, "pdufa calendar", f"{future} upcoming decisions")


async def check_database(checklist: Checklist, config, db) -> None:
    if not await db.health_check():
        checklist.add(FAIL, "database", f"'{config.database.name}' unreachable")
        return
    checklist.add(PASS, "database", config.database.name)

    async with db.session() as session:
        events = (await session.execute(
            select(func.count(Event.id))
        )).scalar() or 0
        checklist.add(PASS, "events table", f"{events} rows")

        blocking = await ErrorRepository(session).unresolved(BLOCKING_ERROR_TYPES)
        if blocking:
            checklist.add(
                FAIL, "critical errors",
                f"{blocking[0].error_type} unresolved — scripts/resolve_errors.py",
            )
        else:
            checklist.add(PASS, "critical errors", "none")

        latest = await EquityRepository(session).get_latest()
        if latest is not None and latest.drawdown_state == DRAWDOWN_HALT:
            checklist.add(FAIL, "drawdown state", "HALT — scripts/reset_halt.py")
        else:
            checklist.add(
                PASS, "drawdown state",
                latest.drawdown_state if latest else "no history",
            )


async def check_broker(checklist: Checklist, config) -> None:
    broker = IBKRClient(config.broker)
    try:
        await broker.connect()
    except Exception as e:
        checklist.add(FAIL, "broker connection", str(e))
        return

    try:
        account = broker.account or ""
        is_paper_account = account.startswith("DU")
        checklist.add(PASS, "broker connection", f"account {account}")

        if config.system.mode == "paper" and not is_paper_account:
            checklist.add(
                FAIL, "account safety",
                f"mode=paper but {account} looks like a LIVE account",
            )
        elif config.system.mode == "live" and is_paper_account:
            checklist.add(
                FAIL, "account safety",
                f"mode=live but {account} is a paper account",
            )
        else:
            checklist.add(PASS, "account safety", f"mode={config.system.mode}")

        summary = await broker.get_account_summary()
        checklist.add(
            PASS, "account equity",
            f"${summary.total_equity:,.2f} at the broker "
            f"(system uses ${config.capital.initial_usd:,.2f} synthetic)",
        )

        try:
            expirations = await broker.get_option_expirations("AAPL")
            if expirations:
                checklist.add(PASS, "option chains", f"{len(expirations)} expirations")
            else:
                checklist.add(WARN, "option chains", "empty — check OPRA subscription")
        except Exception as e:
            checklist.add(WARN, "option chains", f"unavailable: {e}")
    finally:
        await broker.disconnect()


async def main(config_path: str = "config/auriferous.yaml") -> int:
    config = ConfigLoader.load(config_path=config_path)
    checklist = Checklist()

    checklist.add(PASS, "config", f"mode={config.system.mode}")
    if not config.llm.api_key:
        checklist.add(
            FAIL if config.system.mode == "live" else WARN,
            "llm api key",
            "missing — triage and swarm will be disabled",
        )
    else:
        checklist.add(PASS, "llm api key", "present")

    if not config.sentinel.contact_email:
        checklist.add(
            FAIL, "sentinel contact email",
            "SENTINEL_CONTACT_EMAIL missing — SEC requires a contact in the User-Agent",
        )
    else:
        checklist.add(PASS, "sentinel contact email", "present")

    check_universe(checklist)
    check_pdufa(checklist)

    db = DatabaseManager.get_instance(config)
    await check_database(checklist, config, db)
    await check_broker(checklist, config)
    await db.close()

    checklist.render()
    return 0 if checklist.ok else 1


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config/auriferous.yaml"
    sys.exit(asyncio.run(main(path)))
