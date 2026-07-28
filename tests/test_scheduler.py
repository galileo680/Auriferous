from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.scheduler import SchedulerManager


def run(coro):
    return asyncio.run(coro)


def test_job_runs_repeatedly_until_stopped():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()
        calls: list[int] = []

        async def job() -> None:
            calls.append(1)

        scheduler.register("fast", job, interval_seconds=0.05, max_timeout=1.0)
        await scheduler.start()
        await asyncio.sleep(0.28)
        await scheduler.stop(timeout=2.0)
        return len(calls)

    assert run(body()) >= 3


def test_failing_job_keeps_running_and_counts_failures():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()
        attempts: list[int] = []

        async def job() -> None:
            attempts.append(1)
            raise RuntimeError("feed down")

        scheduler.register("flaky", job, interval_seconds=0.05, max_timeout=1.0)
        await scheduler.start()
        await asyncio.sleep(0.22)
        await scheduler.stop(timeout=2.0)
        return len(attempts), scheduler.status()[0]

    attempts, status = run(body())
    assert attempts >= 2
    assert status["failures"] >= 2
    assert status["runs"] == 0
    assert "feed down" in status["last_error"]


def test_one_failing_job_does_not_stop_another():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()
        healthy: list[int] = []

        async def good() -> None:
            healthy.append(1)

        async def bad() -> None:
            raise RuntimeError("boom")

        scheduler.register("good", good, interval_seconds=0.05, max_timeout=1.0)
        scheduler.register("bad", bad, interval_seconds=0.05, max_timeout=1.0)
        await scheduler.start()
        await asyncio.sleep(0.22)
        await scheduler.stop(timeout=2.0)
        return len(healthy)

    assert run(body()) >= 2


def test_timeout_is_recorded_and_job_survives():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()

        async def slow() -> None:
            await asyncio.sleep(5.0)

        scheduler.register("slow", slow, interval_seconds=0.05, max_timeout=0.08)
        await scheduler.start()
        await asyncio.sleep(0.3)
        await scheduler.stop(timeout=2.0)
        return scheduler.status()[0]

    status = run(body())
    assert status["failures"] >= 1
    assert "timed out" in status["last_error"]


def test_disabled_job_never_runs():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()
        calls: list[int] = []

        async def job() -> None:
            calls.append(1)

        scheduler.register("off", job, interval_seconds=0.05, max_timeout=1.0, enabled=False)
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop(timeout=1.0)
        return len(calls)

    assert run(body()) == 0


def test_run_on_start_false_delays_first_execution():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()
        calls: list[int] = []

        async def job() -> None:
            calls.append(1)

        scheduler.register(
            "delayed", job, interval_seconds=5.0, max_timeout=1.0, run_on_start=False
        )
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop(timeout=1.0)
        return len(calls)

    assert run(body()) == 0


def test_duplicate_registration_is_rejected():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()

        async def job() -> None:
            return None

        scheduler.register("dup", job, interval_seconds=1.0, max_timeout=1.0)
        try:
            scheduler.register("dup", job, interval_seconds=1.0, max_timeout=1.0)
            return False
        except ValueError:
            return True

    assert run(body()) is True


def test_stop_is_idempotent():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()

        async def job() -> None:
            return None

        scheduler.register("j", job, interval_seconds=0.05, max_timeout=1.0)
        await scheduler.start()
        await asyncio.sleep(0.1)
        await scheduler.stop(timeout=1.0)
        await scheduler.stop(timeout=1.0)
        return True

    assert run(body()) is True


def test_status_reports_every_registered_job():
    async def body():
        SchedulerManager.reset()
        scheduler = SchedulerManager.get_instance()

        async def job() -> None:
            return None

        scheduler.register("a", job, interval_seconds=1.0, max_timeout=1.0)
        scheduler.register("b", job, interval_seconds=2.0, max_timeout=1.0, enabled=False)
        return scheduler.status()

    status = run(body())
    assert {row["job_id"] for row in status} == {"a", "b"}
    assert [row["enabled"] for row in status if row["job_id"] == "b"] == [False]
