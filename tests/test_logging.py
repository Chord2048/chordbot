from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.log import init_logging, log, shutdown_logging


class LoggingTests(unittest.TestCase):
    def test_jsonl_has_stable_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CHORDCODE_LOG_DIR"] = tmp
            os.environ["CHORDCODE_LOG_CONSOLE"] = "false"
            os.environ["CHORDCODE_LOG_FILE"] = "true"
            os.environ["CHORDCODE_LOG_LEVEL"] = "INFO"
            os.environ["CHORDCODE_LOG_ROTATION"] = "00:00"
            os.environ["CHORDCODE_LOG_RETENTION"] = "7 days"

            init_logging(force=True)
            try:
                log.bind(event="test.event", session_id="s1", message_id="m1").info("hello")
            finally:
                shutdown_logging()

            p = next(Path(tmp).glob("chordcode_*.jsonl"))
            line = p.read_text(encoding="utf-8").splitlines()[0]
            obj = json.loads(line)

            for k in ("ts", "level", "message", "module", "function", "line", "process", "thread", "service", "event", "session_id", "message_id"):
                self.assertIn(k, obj)
            self.assertEqual(obj["message"], "hello")
            self.assertEqual(obj["event"], "test.event")
            self.assertEqual(obj["session_id"], "s1")
            self.assertEqual(obj["message_id"], "m1")

