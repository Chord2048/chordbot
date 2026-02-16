from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.log import init_logging, logger, shutdown_logging


class LoggingTests(unittest.TestCase):
    @staticmethod
    def _read_rows(log_dir: str) -> list[dict]:
        p = next(Path(log_dir).glob("chordcode_*.jsonl"))
        lines = [line for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_jsonl_has_stable_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_logging(level="DEBUG", console=False, file=True, log_dir=tmp, force=True)
            try:
                logger.info("hello", event="test.event", session_id="s1", message_id="m1")
            finally:
                shutdown_logging()

            obj = self._read_rows(tmp)[0]

            for k in ("ts", "level", "message", "module", "function", "line", "process", "thread", "service", "event", "session_id", "message_id"):
                self.assertIn(k, obj)
            self.assertEqual(obj["message"], "hello")
            self.assertEqual(obj["event"], "test.event")
            self.assertEqual(obj["session_id"], "s1")
            self.assertEqual(obj["message_id"], "m1")

    def test_error_with_exc_info_outputs_exception_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_logging(level="DEBUG", console=False, file=True, log_dir=tmp, force=True)
            try:
                try:
                    raise ValueError("boom")
                except ValueError as e:
                    logger.error("failed", event="test.error", exc_info=e)
            finally:
                shutdown_logging()

            obj = self._read_rows(tmp)[0]
            self.assertEqual(obj["event"], "test.error")
            self.assertIn("exception", obj)
            ex = obj["exception"]
            self.assertEqual(ex["type"], "ValueError")
            self.assertEqual(ex["message"], "boom")

    def test_context_scoped_to_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_logging(level="DEBUG", console=False, file=True, log_dir=tmp, force=True)
            try:
                with logger.context(session_id="s-ctx", event="ctx.event"):
                    logger.info("inside")
                logger.info("outside", event="outside.event")
            finally:
                shutdown_logging()

            rows = self._read_rows(tmp)
            inside = next(row for row in rows if row["message"] == "inside")
            outside = next(row for row in rows if row["message"] == "outside")
            self.assertEqual(inside["session_id"], "s-ctx")
            self.assertEqual(inside["event"], "ctx.event")
            self.assertNotIn("session_id", outside)
            self.assertEqual(outside["event"], "outside.event")

    def test_child_logger_merges_fields_with_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_logging(level="DEBUG", console=False, file=True, log_dir=tmp, force=True)
            try:
                child = logger.child(agent="worker", component="planner")
                with child.context(session_id="s-child"):
                    child.info("child log", event="child.event")
            finally:
                shutdown_logging()

            obj = self._read_rows(tmp)[0]
            self.assertEqual(obj["agent"], "worker")
            self.assertEqual(obj["session_id"], "s-child")
            self.assertEqual(obj["event"], "child.event")
            self.assertEqual(obj["extra"]["component"], "planner")

    def test_context_isolation_across_async_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            async def emit(sid: str, delay: float) -> None:
                with logger.context(session_id=sid, event="async.event"):
                    await asyncio.sleep(delay)
                    logger.info("async message")

            async def run_all() -> None:
                await asyncio.gather(emit("s-1", 0.02), emit("s-2", 0.01))

            init_logging(level="DEBUG", console=False, file=True, log_dir=tmp, force=True)
            try:
                asyncio.run(run_all())
            finally:
                shutdown_logging()

            rows = [row for row in self._read_rows(tmp) if row.get("message") == "async message"]
            self.assertEqual(len(rows), 2)
            self.assertEqual({row.get("session_id") for row in rows}, {"s-1", "s-2"})
