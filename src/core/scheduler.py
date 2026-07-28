from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

import structlog

from src.core.clock import utcnow

JobCallback = Callable[[], Awaitable[None]]


@dataclass
class ScheduledJob:
    job_id: str
    callback: JobCallback
    interval_seconds: float
    max_timeout: float
    enabled: bool = True
    run_on_start: bool = True
    runs: int = 0
    failures: int = 0
    last_run_at: Optional[datetime] = None
    last_error: Optional[str] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "runs": self.runs,
            "failures": self.failures,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
        }


class SchedulerManager:
    _instance: Optional["SchedulerManager"] = None

    def __init__(self) -> None:
        self._jobs: dict[str, ScheduledJob] = {}
        self._shutdown = asyncio.Event()
        self._running = False
        self._logger = structlog.get_logger("Scheduler")

    @classmethod
    def get_instance(cls) -> "SchedulerManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def register(
        self,
        job_id: str,
        callback: JobCallback,
        interval_seconds: float,
        max_timeout: float,
        enabled: bool = True,
        run_on_start: bool = True,
    ) -> None:
        if job_id in self._jobs:
            raise ValueError(f"Job already registered: {job_id}")
        if max_timeout > interval_seconds:
            self._logger.warning(
                "job_timeout_exceeds_interval",
                job_id=job_id,
                interval_seconds=interval_seconds,
                max_timeout=max_timeout,
                note="a slow run will delay the next one",
            )

        self._jobs[job_id] = ScheduledJob(
            job_id=job_id,
            callback=callback,
            interval_seconds=interval_seconds,
            max_timeout=max_timeout,
            enabled=enabled,
            run_on_start=run_on_start,
        )
        self._logger.info(
            "job_registered",
            job_id=job_id,
            interval_seconds=interval_seconds,
            enabled=enabled,
        )

    def status(self) -> list[dict]:
        return [job.to_dict() for job in self._jobs.values()]

    async def start(self) -> None:
        if self._running:
            return

        self._shutdown.clear()
        self._running = True

        for job in self._jobs.values():
            if not job.enabled:
                self._logger.info("job_disabled", job_id=job.job_id)
                continue
            job.task = asyncio.create_task(self._run_job(job), name=f"job:{job.job_id}")

        active = [j.job_id for j in self._jobs.values() if j.enabled]
        self._logger.info("scheduler_started", jobs=active)

    async def _run_job(self, job: ScheduledJob) -> None:
        if not job.run_on_start:
            if await self._sleep_or_shutdown(job.interval_seconds):
                return

        while not self._shutdown.is_set():
            started = utcnow()
            try:
                await asyncio.wait_for(job.callback(), timeout=job.max_timeout)
                job.runs += 1
                job.last_error = None
            except asyncio.TimeoutError:
                job.failures += 1
                job.last_error = f"timed out after {job.max_timeout}s"
                self._logger.error(
                    "job_timeout", job_id=job.job_id, max_timeout=job.max_timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                job.failures += 1
                job.last_error = str(e)
                self._logger.error("job_failed", job_id=job.job_id, error=str(e))
            finally:
                job.last_run_at = started

            elapsed = (utcnow() - started).total_seconds()
            remaining = max(job.interval_seconds - elapsed, 0.0)
            if await self._sleep_or_shutdown(remaining):
                return

    async def _sleep_or_shutdown(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._shutdown.is_set()
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_for_shutdown(self) -> None:
        await self._shutdown.wait()

    async def stop(self, timeout: float = 30.0) -> None:
        if not self._running:
            return

        self._logger.info("scheduler_stopping")
        self._shutdown.set()

        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        self._running = False
        self._logger.info(
            "scheduler_stopped",
            summary=[job.to_dict() for job in self._jobs.values()],
        )
