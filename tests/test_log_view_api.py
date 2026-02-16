from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))


class LogViewApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._db_tmp = tempfile.TemporaryDirectory()
        cls._config_tmp = tempfile.TemporaryDirectory()

        # Create a temporary config file for the app
        import yaml
        config_dir = Path(cls._config_tmp.name) / ".chordcode"
        config_dir.mkdir(parents=True)
        config_data = {
            "openai": {
                "base_url": "http://local.test/v1",
                "api_key": "test-key",
                "model": "test-model",
            },
            "langfuse": {"enabled": False},
            "logging": {"console": False, "file": False},
            "db_path": str(Path(cls._db_tmp.name) / "test.sqlite3"),
            "default_worktree": cls._config_tmp.name,
        }
        (config_dir / "config.yaml").write_text(yaml.dump(config_data))

        # Patch config.load to use our temp config
        import chordcode.config as _cfg_mod
        cls._orig_global_paths = _cfg_mod.GLOBAL_CONFIG_PATHS
        _cfg_mod.GLOBAL_CONFIG_PATHS = (str(config_dir / "config.yaml"),)

        if "chordcode.api.app" in sys.modules:
            del sys.modules["chordcode.api.app"]
        cls._api_mod = importlib.import_module("chordcode.api.app")
        cls.client = TestClient(cls._api_mod.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._db_tmp.cleanup()
        cls._config_tmp.cleanup()
        # Restore global config paths
        import chordcode.config as _cfg_mod
        _cfg_mod.GLOBAL_CONFIG_PATHS = cls._orig_global_paths

    def setUp(self) -> None:
        self._logs_tmp = tempfile.TemporaryDirectory()
        # Patch the app module's cfg.logging.dir to use our temp log dir
        _app_mod = self.__class__._api_mod
        from chordcode.config import LoggingConfig
        old_logging = _app_mod.cfg.logging
        self._old_cfg = _app_mod.cfg
        # Create a new Config with updated logging.dir
        from dataclasses import replace as _dc_replace
        new_logging = LoggingConfig(
            level=old_logging.level,
            console=old_logging.console,
            file=old_logging.file,
            dir=self._logs_tmp.name,
            rotation=old_logging.rotation,
            retention=old_logging.retention,
        )
        _app_mod.cfg = _dc_replace(_app_mod.cfg, logging=new_logging)

    def tearDown(self) -> None:
        _app_mod = self.__class__._api_mod
        _app_mod.cfg = self._old_cfg
        self._logs_tmp.cleanup()

    def _write_jsonl(self, date: str, rows: list[dict | str]) -> Path:
        path = Path(self._logs_tmp.name) / f"chordcode_{date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                if isinstance(row, str):
                    f.write(row + "\n")
                else:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def test_list_log_files_empty(self) -> None:
        res = self.client.get("/logs/files")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["files"], [])
        self.assertIsNone(data["default_date"])
        self.assertEqual(Path(data["log_dir"]).resolve(), Path(self._logs_tmp.name).resolve())

    def test_list_log_files_sorted_desc(self) -> None:
        p1 = self._write_jsonl("2026-02-04", [{"ts": "2026-02-04T00:00:00Z", "level": "INFO"}])
        p2 = self._write_jsonl("2026-02-09", [{"ts": "2026-02-09T00:00:00Z", "level": "INFO"}])
        (Path(self._logs_tmp.name) / "not-a-log.txt").write_text("x", encoding="utf-8")

        res = self.client.get("/logs/files")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["default_date"], "2026-02-09")
        self.assertEqual([x["date"] for x in data["files"]], ["2026-02-09", "2026-02-04"])
        self.assertEqual(data["files"][0]["name"], p2.name)
        self.assertEqual(data["files"][1]["name"], p1.name)
        self.assertGreater(data["files"][0]["size"], 0)
        self.assertGreater(data["files"][0]["mtime"], 0)

    def test_logs_pagination_and_filters(self) -> None:
        self._write_jsonl(
            "2026-02-09",
            [
                {
                    "ts": "2026-02-09T10:00:00Z",
                    "level": "INFO",
                    "event": "session.status",
                    "session_id": "s1",
                    "message": "alpha",
                    "module": "a",
                    "function": "f1",
                },
                {
                    "ts": "2026-02-09T10:01:00Z",
                    "level": "ERROR",
                    "event": "tool.error",
                    "session_id": "s2",
                    "message": "boom keyword",
                    "module": "b",
                    "function": "f2",
                },
                "not-json",
                {
                    "ts": "2026-02-09T10:02:00Z",
                    "level": "INFO",
                    "event": "message.updated",
                    "session_id": "s1",
                    "message": "beta keyword",
                    "module": "c",
                    "function": "f3",
                },
            ],
        )

        first = self.client.get("/logs", params={"date": "2026-02-09", "limit": 1})
        self.assertEqual(first.status_code, 200)
        first_data = first.json()
        self.assertEqual(first_data["total"], 3)
        self.assertEqual(first_data["offset"], 0)
        self.assertEqual(first_data["limit"], 1)
        self.assertTrue(first_data["has_more"])
        self.assertEqual(len(first_data["items"]), 1)
        self.assertEqual(first_data["items"][0]["line_no"], 4)

        second = self.client.get("/logs", params={"date": "2026-02-09", "offset": 1, "limit": 1})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["items"][0]["line_no"], 2)

        filtered = self.client.get(
            "/logs",
            params={
                "date": "2026-02-09",
                "level": "info",
                "event": "message",
                "session_id": "s1",
                "q": "keyword",
            },
        )
        self.assertEqual(filtered.status_code, 200)
        filtered_data = filtered.json()
        self.assertEqual(filtered_data["total"], 1)
        self.assertEqual(filtered_data["items"][0]["event"], "message.updated")
        self.assertEqual(filtered_data["items"][0]["session_id"], "s1")

    def test_logs_invalid_date_returns_400(self) -> None:
        res = self.client.get("/logs", params={"date": "2026-2-9"})
        self.assertEqual(res.status_code, 400)

    def test_logs_missing_file_returns_404(self) -> None:
        res = self.client.get("/logs", params={"date": "2026-02-09"})
        self.assertEqual(res.status_code, 404)
