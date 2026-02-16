from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))


class SessionManagementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._db_tmp = tempfile.TemporaryDirectory()
        cls._worktree_tmp = tempfile.TemporaryDirectory()
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
            "default_worktree": cls._worktree_tmp.name,
        }
        (config_dir / "config.yaml").write_text(yaml.dump(config_data))

        # Patch config.load to use our temp config
        import chordcode.config as _cfg_mod
        cls._orig_global_paths = _cfg_mod.GLOBAL_CONFIG_PATHS
        _cfg_mod.GLOBAL_CONFIG_PATHS = (str(config_dir / "config.yaml"),)

        if "chordcode.api.app" in sys.modules:
            del sys.modules["chordcode.api.app"]
        api_app = importlib.import_module("chordcode.api.app")
        cls.client = TestClient(api_app.app)
        cls.client.__enter__()
        cls.db_path = str(Path(cls._db_tmp.name) / "test.sqlite3")
        cls.worktree = cls._worktree_tmp.name

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls._db_tmp.cleanup()
        cls._worktree_tmp.cleanup()
        cls._config_tmp.cleanup()
        # Restore global config paths
        import chordcode.config as _cfg_mod
        _cfg_mod.GLOBAL_CONFIG_PATHS = cls._orig_global_paths

    def setUp(self) -> None:
        self._clear_all_tables()

    def _clear_all_tables(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM parts")
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM permission_requests")
            db.execute("DELETE FROM permission_approvals")
            db.execute("DELETE FROM todos")
            db.execute("DELETE FROM sessions")
            db.commit()

    def _create_session(self, title: str = "Original") -> dict:
        res = self.client.post(
            "/sessions",
            json={
                "worktree": self.worktree,
                "title": title,
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_rename_session(self) -> None:
        session = self._create_session("Before Rename")
        session_id = session["id"]
        before_updated_at = int(session["updated_at"])

        time.sleep(0.01)  # Ensure updated_at has a chance to move forward.
        res = self.client.patch(f"/sessions/{session_id}", json={"title": "After Rename"})
        self.assertEqual(res.status_code, 200, res.text)
        updated = res.json()

        self.assertEqual(updated["id"], session_id)
        self.assertEqual(updated["title"], "After Rename")
        self.assertGreaterEqual(int(updated["updated_at"]), before_updated_at)

        listed = self.client.get("/sessions")
        self.assertEqual(listed.status_code, 200, listed.text)
        listed_sessions = listed.json()["sessions"]
        self.assertEqual(len(listed_sessions), 1)
        self.assertEqual(listed_sessions[0]["title"], "After Rename")

    def test_rename_session_requires_non_empty_title(self) -> None:
        session = self._create_session("Keep Name")
        session_id = session["id"]

        res = self.client.patch(f"/sessions/{session_id}", json={"title": "   "})
        self.assertEqual(res.status_code, 400)

    def test_delete_session_cascades_related_rows(self) -> None:
        session = self._create_session("To Be Deleted")
        session_id = session["id"]

        add_msg_res = self.client.post(
            f"/sessions/{session_id}/messages",
            json={"text": "hello"},
        )
        self.assertEqual(add_msg_res.status_code, 200, add_msg_res.text)

        now = int(time.time() * 1000)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO todos (id, session_id, content, status, priority, active_form, position, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("todo-1", session_id, "task", "pending", "medium", "Working...", 0, now, now),
            )
            db.execute(
                """
                INSERT INTO permission_requests (
                    id, session_id, permission, patterns_json, metadata_json, always_json, tool_json, status, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "req-1",
                    session_id,
                    "read",
                    json.dumps(["*"]),
                    json.dumps({}),
                    json.dumps([]),
                    None,
                    "pending",
                    now,
                    None,
                ),
            )
            db.execute(
                """
                INSERT INTO permission_approvals (session_id, permission, pattern, action)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, "read", "*", "allow"),
            )
            db.commit()

        delete_res = self.client.delete(f"/sessions/{session_id}")
        self.assertEqual(delete_res.status_code, 200, delete_res.text)
        payload = delete_res.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session_id"], session_id)

        listed = self.client.get("/sessions")
        self.assertEqual(listed.status_code, 200, listed.text)
        session_ids = [s["id"] for s in listed.json()["sessions"]]
        self.assertNotIn(session_id, session_ids)

        with sqlite3.connect(self.db_path) as db:
            def count_rows(table: str) -> int:
                cur = db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,))
                return int(cur.fetchone()[0])

            self.assertEqual(count_rows("messages"), 0)
            self.assertEqual(count_rows("parts"), 0)
            self.assertEqual(count_rows("todos"), 0)
            self.assertEqual(count_rows("permission_requests"), 0)
            self.assertEqual(count_rows("permission_approvals"), 0)
            cur = db.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (session_id,))
            self.assertEqual(int(cur.fetchone()[0]), 0)

    def test_delete_missing_session_returns_404(self) -> None:
        res = self.client.delete("/sessions/not-found")
        self.assertEqual(res.status_code, 404)
