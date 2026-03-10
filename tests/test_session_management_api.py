from __future__ import annotations

import importlib
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

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
            db.execute("DELETE FROM cron_job_runs")
            db.execute("DELETE FROM cron_jobs")
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

    def test_create_session_defaults_to_local_runtime(self) -> None:
        session = self._create_session("Runtime Default")
        self.assertEqual(session.get("runtime", {}).get("backend"), "local")

    def test_create_daytona_session_returns_sandbox_id(self) -> None:
        from chordcode.model import SessionRuntime, DaytonaRuntimeConfig

        async def _ensure(session):
            return session.model_copy(
                update={
                    "runtime": SessionRuntime(
                        backend="daytona",
                        daytona=DaytonaRuntimeConfig(sandbox_id="sbx-test", sandbox_name="demo-sandbox"),
                    )
                },
            )

        with patch("chordcode.api.app.daytona_manager.ensure_session_runtime_async", new=AsyncMock(side_effect=_ensure)) as mocked:
            res = self.client.post(
                "/sessions",
                json={
                    "worktree": "/workspace",
                    "title": "Daytona Session",
                    "runtime": {"backend": "daytona"},
                },
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["runtime"]["backend"], "daytona")
            self.assertEqual(data["runtime"]["daytona"]["sandbox_id"], "sbx-test")
            self.assertEqual(data["runtime"]["daytona"]["sandbox_name"], "demo-sandbox")
            self.assertEqual(mocked.await_count, 1)

    def test_create_daytona_session_uses_remote_default_workspace_when_local_default_passed(self) -> None:
        from chordcode.model import SessionRuntime, DaytonaRuntimeConfig

        async def _ensure(session):
            # Ensure API normalized worktree before runtime init.
            self.assertEqual(session.worktree, "/workspace")
            self.assertEqual(session.cwd, "/workspace")
            return session.model_copy(
                update={
                    "runtime": SessionRuntime(
                        backend="daytona",
                        daytona=DaytonaRuntimeConfig(sandbox_id="sbx-remote", sandbox_name="remote-name"),
                    ),
                },
            )

        with patch("chordcode.api.app.daytona_manager.ensure_session_runtime_async", new=AsyncMock(side_effect=_ensure)):
            res = self.client.post(
                "/sessions",
                json={
                    "worktree": self.worktree,
                    "title": "Daytona Session",
                    "runtime": {"backend": "daytona"},
                },
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["worktree"], "/workspace")
            self.assertEqual(data["cwd"], "/workspace")

    def test_create_daytona_session_rolls_back_when_runtime_init_fails(self) -> None:
        from chordcode.runtime import DaytonaOperationError

        with patch(
            "chordcode.api.app.daytona_manager.ensure_session_runtime_async",
            new=AsyncMock(side_effect=DaytonaOperationError("daytona failed")),
        ):
            res = self.client.post(
                "/sessions",
                json={
                    "worktree": "/workspace",
                    "title": "Bad Daytona",
                    "runtime": {"backend": "daytona"},
                },
            )
            self.assertEqual(res.status_code, 502, res.text)

        listed = self.client.get("/sessions")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json().get("sessions", [])), 0)

    def test_build_tools_switches_by_runtime_backend(self) -> None:
        app_module = importlib.import_module("chordcode.api.app")
        from chordcode.model import SessionRuntime, DaytonaRuntimeConfig
        from chordcode.tools.bash import BashTool
        from chordcode.tools.daytona import DaytonaBashTool

        session = self._create_session("Tool Runtime")
        session_obj = asyncio.run(app_module.store.get_session(session["id"]))
        tools_local = asyncio.run(app_module._build_tools(session_obj))
        self.assertIsInstance(tools_local.get("bash"), BashTool)

        session_daytona = session_obj.model_copy(
            update={"runtime": SessionRuntime(backend="daytona", daytona=DaytonaRuntimeConfig(sandbox_id="sbx-1"))},
        )
        fake_sandbox_ref = SimpleNamespace(sandbox_id="sbx-1", sandbox=SimpleNamespace(process=SimpleNamespace(), fs=SimpleNamespace()))
        with patch(
            "chordcode.api.app.daytona_manager.get_sandbox_for_session",
            new=AsyncMock(return_value=fake_sandbox_ref),
        ):
            tools_daytona = asyncio.run(app_module._build_tools(session_daytona))
            self.assertIsInstance(tools_daytona.get("bash"), DaytonaBashTool)

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
            db.execute(
                """
                INSERT INTO cron_jobs (
                    id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                    payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error, last_assistant_message_id,
                    last_trace_id, delete_after_run, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-1",
                    "cleanup-test",
                    session_id,
                    1,
                    "every",
                    None,
                    60000,
                    None,
                    None,
                    "agent_turn",
                    "ping",
                    now + 60000,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO cron_job_runs (job_id, session_id, started_at, finished_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("job-1", session_id, now, now + 10, "ok"),
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
            self.assertEqual(count_rows("cron_jobs"), 0)
            self.assertEqual(count_rows("cron_job_runs"), 0)
            cur = db.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (session_id,))
            self.assertEqual(int(cur.fetchone()[0]), 0)

    def test_delete_missing_session_returns_404(self) -> None:
        res = self.client.delete("/sessions/not-found")
        self.assertEqual(res.status_code, 404)

    def test_channels_status_endpoint(self) -> None:
        res = self.client.get("/channels/status")
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertIn("enabled_channels", data)
        self.assertIn("channels", data)
        self.assertIn("bridge_running", data)
        self.assertIn("queue", data)

    def test_channels_config_and_manage_endpoints(self) -> None:
        get_res = self.client.get("/channels/config")
        self.assertEqual(get_res.status_code, 200, get_res.text)
        cfg = get_res.json()
        self.assertIn("channels", cfg)
        self.assertIn("feishu", cfg["channels"])

        put_res = self.client.put(
            "/channels/config/feishu",
            json={
                "enabled": False,
                "app_id": "cli_test",
                "app_secret": "",
                "encrypt_key": "",
                "verification_token": "",
                "allow_from": ["ou_1", "ou_2"],
                "permission_mode": "commands",
                "allowed_bash_commands": ["git status", "ls *"],
            },
        )
        self.assertEqual(put_res.status_code, 200, put_res.text)
        put_data = put_res.json()
        self.assertTrue(put_data["ok"])
        self.assertIn("channels", put_data)
        self.assertIn("feishu", put_data["channels"])
        self.assertEqual(put_data["channels"]["feishu"]["app_id"], "cli_test")
        self.assertEqual(put_data["channels"]["feishu"]["allow_from"], ["ou_1", "ou_2"])
        self.assertEqual(put_data["channels"]["feishu"]["permission_mode"], "commands")
        self.assertEqual(put_data["channels"]["feishu"]["allowed_bash_commands"], ["git status", "ls *"])

        partial_res = self.client.put(
            "/channels/config/feishu",
            json={"permission_mode": "allow"},
        )
        self.assertEqual(partial_res.status_code, 200, partial_res.text)
        partial_data = partial_res.json()
        self.assertEqual(partial_data["channels"]["feishu"]["permission_mode"], "allow")
        self.assertEqual(partial_data["channels"]["feishu"]["app_id"], "cli_test")

        bad_mode_res = self.client.put(
            "/channels/config/feishu",
            json={"permission_mode": "invalid"},
        )
        self.assertEqual(bad_mode_res.status_code, 400, bad_mode_res.text)

        connect_res = self.client.post("/channels/feishu/connect")
        self.assertEqual(connect_res.status_code, 404, connect_res.text)

        disconnect_res = self.client.post("/channels/feishu/disconnect")
        self.assertEqual(disconnect_res.status_code, 404, disconnect_res.text)

        test_res = self.client.post("/channels/feishu/test")
        self.assertEqual(test_res.status_code, 404, test_res.text)

    def test_cronjobs_crud_endpoints(self) -> None:
        session = self._create_session("Cron Session")

        create_res = self.client.post(
            "/cronjobs",
            json={
                "name": "Heartbeat",
                "session_id": session["id"],
                "message": "please summarize latest progress",
                "schedule": {"kind": "every", "every_ms": 3600000},
            },
        )
        self.assertEqual(create_res.status_code, 200, create_res.text)
        job = create_res.json()
        job_id = job["id"]
        self.assertEqual(job["name"], "Heartbeat")
        self.assertEqual(job["session_id"], session["id"])
        self.assertTrue(job["enabled"])
        self.assertIsNotNone(job["state"]["next_run_at_ms"])

        list_res = self.client.get("/cronjobs")
        self.assertEqual(list_res.status_code, 200, list_res.text)
        jobs = list_res.json()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], job_id)

        get_res = self.client.get(f"/cronjobs/{job_id}")
        self.assertEqual(get_res.status_code, 200, get_res.text)
        self.assertEqual(get_res.json()["id"], job_id)

        disable_res = self.client.post(f"/cronjobs/{job_id}/enabled", json={"enabled": False})
        self.assertEqual(disable_res.status_code, 200, disable_res.text)
        disabled = disable_res.json()
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["state"]["next_run_at_ms"])

        runs_res = self.client.get(f"/cronjobs/{job_id}/runs")
        self.assertEqual(runs_res.status_code, 200, runs_res.text)
        self.assertEqual(runs_res.json()["runs"], [])

        delete_res = self.client.delete(f"/cronjobs/{job_id}")
        self.assertEqual(delete_res.status_code, 200, delete_res.text)
        self.assertTrue(delete_res.json()["ok"])

        missing_res = self.client.get(f"/cronjobs/{job_id}")
        self.assertEqual(missing_res.status_code, 404, missing_res.text)

    def test_cronjobs_status_and_validation(self) -> None:
        status_res = self.client.get("/cronjobs/status")
        self.assertEqual(status_res.status_code, 200, status_res.text)
        status = status_res.json()
        self.assertIn("running", status)
        self.assertIn("jobs", status)
        self.assertIn("next_wake_at_ms", status)

        session = self._create_session("Cron Validation")
        bad_res = self.client.post(
            "/cronjobs",
            json={
                "name": "Bad Cron",
                "session_id": session["id"],
                "message": "hello",
                "schedule": {"kind": "every"},
            },
        )
        self.assertEqual(bad_res.status_code, 400, bad_res.text)
