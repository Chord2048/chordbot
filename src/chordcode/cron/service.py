from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from chordcode.log import logger
from chordcode.model import CronJob, CronSchedule
from chordcode.store.sqlite import SQLiteStore


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class CronJobExecResult:
    assistant_message_id: str | None = None
    trace_id: str | None = None


class CronService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        on_job: Callable[[CronJob], Awaitable[CronJobExecResult | None]],
        poll_interval_sec: float = 2.0,
        max_jobs_per_tick: int = 8,
    ) -> None:
        self._store = store
        self._on_job = on_job
        self._poll_interval_sec = max(0.05, poll_interval_sec)
        self._max_jobs_per_tick = max(1, max_jobs_per_tick)
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running = False
        self._executing_job_ids: set[str] = set()
        self._guard = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        async with self._guard:
            if self._running:
                return
            self._running = True
            await self._repair_enabled_jobs()
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Cron service started", event="cron.start", poll_interval_sec=self._poll_interval_sec)

    async def stop(self) -> None:
        async with self._guard:
            self._running = False
            self._wake_event.set()
            task = self._task
            self._task = None
        if task:
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except Exception as exc:
                logger.warning("Cron service stop wait failed", event="cron.stop.wait.error", error=str(exc))
        logger.info("Cron service stopped", event="cron.stop")

    def notify_changed(self) -> None:
        self._wake_event.set()

    async def status(self) -> dict[str, int | bool | None]:
        return {
            "running": self._running,
            "next_wake_at_ms": await self._store.get_next_cron_wake_ms(),
            "jobs": len(await self._store.list_cron_jobs()),
            "executing_jobs": len(self._executing_job_ids),
        }

    async def create_job(self, job: CronJob) -> CronJob:
        self.validate_schedule(job.schedule)
        await self._store.get_session(job.session_id)
        if job.enabled:
            job.state.next_run_at_ms = self.compute_next_run(job.schedule, _now_ms())
        else:
            job.state.next_run_at_ms = None
        await self._store.create_cron_job(job)
        self.notify_changed()
        return await self._store.get_cron_job(job.id)

    async def set_job_enabled(self, job_id: str, *, enabled: bool) -> CronJob:
        job = await self._store.get_cron_job(job_id)
        next_run = self.compute_next_run(job.schedule, _now_ms()) if enabled else None
        updated = await self._store.update_cron_job_enabled(job_id, enabled=enabled, next_run_at_ms=next_run)
        self.notify_changed()
        return updated

    async def delete_job(self, job_id: str) -> bool:
        deleted = await self._store.delete_cron_job(job_id)
        if deleted:
            self.notify_changed()
        return deleted

    async def run_job(self, job_id: str, *, force: bool = False) -> bool:
        job = await self._store.get_cron_job(job_id)
        if not force and not job.enabled:
            return False
        await self._execute_job(job)
        self.notify_changed()
        return True

    async def _run_loop(self) -> None:
        while self._running:
            try:
                now = _now_ms()
                due_jobs = await self._store.list_due_cron_jobs(now, limit=self._max_jobs_per_tick)
                if due_jobs:
                    for job in due_jobs:
                        await self._execute_job(job)
                    continue

                next_wake = await self._store.get_next_cron_wake_ms()
                timeout = self._poll_interval_sec
                if next_wake is not None:
                    timeout = max(0.05, min(timeout, max(0.0, (next_wake - now) / 1000.0)))
                await self._wait_with_wakeup(timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Cron loop tick failed", event="cron.tick.error", error=str(exc))
                await self._wait_with_wakeup(self._poll_interval_sec)

    async def _wait_with_wakeup(self, timeout: float) -> None:
        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    async def _repair_enabled_jobs(self) -> None:
        now = _now_ms()
        jobs = await self._store.list_cron_jobs(include_disabled=False)
        for job in jobs:
            if job.state.next_run_at_ms is None:
                next_run = self.compute_next_run(job.schedule, now)
                await self._store.update_cron_job_runtime(
                    job.id,
                    next_run_at_ms=next_run,
                    last_run_at_ms=job.state.last_run_at_ms,
                    last_status=job.state.last_status,
                    last_error=job.state.last_error,
                    last_assistant_message_id=job.state.last_assistant_message_id,
                    last_trace_id=job.state.last_trace_id,
                    enabled=job.enabled,
                )

    async def _execute_job(self, job: CronJob) -> None:
        if job.id in self._executing_job_ids:
            return

        self._executing_job_ids.add(job.id)
        try:
            started_at = _now_ms()
            run_id = await self._store.create_cron_job_run(job_id=job.id, session_id=job.session_id, started_at_ms=started_at)

            status = "ok"
            error: str | None = None
            assistant_message_id: str | None = None
            trace_id: str | None = None

            logger.info("Cron job executing", event="cron.job.start", job_id=job.id, session_id=job.session_id, name=job.name)
            try:
                result = await self._on_job(job)
                if result:
                    assistant_message_id = result.assistant_message_id
                    trace_id = result.trace_id
            except Exception as exc:
                status = "error"
                error = str(exc)
                logger.error(
                    "Cron job execution failed",
                    event="cron.job.error",
                    job_id=job.id,
                    session_id=job.session_id,
                    error=error,
                )

            finished_at = _now_ms()
            await self._store.finish_cron_job_run(
                run_id,
                status=status,
                finished_at_ms=finished_at,
                error=error,
                assistant_message_id=assistant_message_id,
                trace_id=trace_id,
            )

            enabled = job.enabled
            next_run: int | None = None

            if job.schedule.kind == "at":
                enabled = False
                next_run = None
                if job.delete_after_run:
                    await self._store.delete_cron_job(job.id)
                    logger.info("Cron one-shot job deleted after run", event="cron.job.deleted", job_id=job.id)
                    return
            else:
                next_run = self.compute_next_run(job.schedule, finished_at) if enabled else None

            await self._store.update_cron_job_runtime(
                job.id,
                next_run_at_ms=next_run,
                last_run_at_ms=started_at,
                last_status=status,
                last_error=error,
                last_assistant_message_id=assistant_message_id,
                last_trace_id=trace_id,
                enabled=enabled,
            )
            logger.info(
                "Cron job finished",
                event="cron.job.finish",
                job_id=job.id,
                session_id=job.session_id,
                status=status,
                next_run_at_ms=next_run,
            )
        finally:
            self._executing_job_ids.discard(job.id)

    @staticmethod
    def validate_schedule(schedule: CronSchedule) -> None:
        if schedule.tz and schedule.kind != "cron":
            raise ValueError("tz can only be used with cron schedule")

        if schedule.kind == "at":
            if not schedule.at_ms or schedule.at_ms <= 0:
                raise ValueError("at schedule requires positive at_ms")
            return

        if schedule.kind == "every":
            if not schedule.every_ms or schedule.every_ms <= 0:
                raise ValueError("every schedule requires positive every_ms")
            return

        if schedule.kind == "cron":
            if not (schedule.expr or "").strip():
                raise ValueError("cron schedule requires expr")
            if schedule.tz:
                try:
                    ZoneInfo(schedule.tz)
                except Exception as exc:
                    raise ValueError(f"invalid timezone: {schedule.tz}") from exc
            try:
                from croniter import croniter

                croniter(schedule.expr, datetime.now())
            except Exception as exc:
                raise ValueError(f"invalid cron expr: {schedule.expr}") from exc
            return

        raise ValueError(f"unknown schedule kind: {schedule.kind}")

    @staticmethod
    def compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
        if schedule.kind == "at":
            if schedule.at_ms and schedule.at_ms > now_ms:
                return schedule.at_ms
            return None

        if schedule.kind == "every":
            if not schedule.every_ms or schedule.every_ms <= 0:
                return None
            return now_ms + schedule.every_ms

        if schedule.kind == "cron":
            if not schedule.expr:
                return None
            try:
                from croniter import croniter
            except Exception as exc:
                raise RuntimeError("croniter dependency is required for cron expressions") from exc

            tzinfo = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=tzinfo)
            next_dt = croniter(schedule.expr, base_dt).get_next(datetime)
            return int(next_dt.timestamp() * 1000)

        return None
