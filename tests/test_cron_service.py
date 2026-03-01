from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.cron import CronJobExecResult, CronService
from chordcode.model import CronJob, CronPayload, CronSchedule, PermissionRule, Session
from chordcode.store.sqlite import SQLiteStore


class CronServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_due_every_job_and_records_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()

            await store.create_session(
                Session(
                    id="s1",
                    title="cron session",
                    worktree=tmp,
                    cwd=tmp,
                    created_at=1,
                    updated_at=1,
                    permission_rules=[PermissionRule(permission="*", pattern="*", action="deny")],
                )
            )

            triggered = asyncio.Event()

            async def on_job(job: CronJob) -> CronJobExecResult:
                triggered.set()
                return CronJobExecResult(assistant_message_id="m1", trace_id="t1")

            svc = CronService(store=store, on_job=on_job, poll_interval_sec=0.05, max_jobs_per_tick=4)
            now = int(time.time() * 1000)
            await svc.create_job(
                CronJob(
                    id="job1",
                    name="every job",
                    session_id="s1",
                    enabled=True,
                    schedule=CronSchedule(kind="every", every_ms=60),
                    payload=CronPayload(message="ping"),
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )

            await svc.start()
            try:
                await asyncio.wait_for(triggered.wait(), timeout=1.2)
            finally:
                await svc.stop()

            job = await store.get_cron_job("job1")
            self.assertEqual(job.state.last_status, "ok")
            self.assertEqual(job.state.last_assistant_message_id, "m1")
            self.assertIsNotNone(job.state.next_run_at_ms)

            runs = await store.list_cron_job_runs("job1", limit=10)
            self.assertGreaterEqual(len(runs), 1)
            self.assertEqual(runs[0].status, "ok")
            self.assertEqual(runs[0].assistant_message_id, "m1")

    async def test_manual_run_respects_force_for_disabled_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.sqlite3")
            store = SQLiteStore(db_path)
            await store.init()

            await store.create_session(
                Session(
                    id="s1",
                    title="cron session",
                    worktree=tmp,
                    cwd=tmp,
                    created_at=1,
                    updated_at=1,
                    permission_rules=[PermissionRule(permission="*", pattern="*", action="deny")],
                )
            )

            async def on_job(job: CronJob) -> CronJobExecResult:
                return CronJobExecResult(assistant_message_id="m2", trace_id=None)

            svc = CronService(store=store, on_job=on_job, poll_interval_sec=0.05)
            now = int(time.time() * 1000)
            await svc.create_job(
                CronJob(
                    id="job2",
                    name="disabled",
                    session_id="s1",
                    enabled=False,
                    schedule=CronSchedule(kind="every", every_ms=5000),
                    payload=CronPayload(message="ping"),
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )

            ran_without_force = await svc.run_job("job2", force=False)
            self.assertFalse(ran_without_force)

            ran_with_force = await svc.run_job("job2", force=True)
            self.assertTrue(ran_with_force)
            runs = await store.list_cron_job_runs("job2", limit=5)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].status, "ok")
