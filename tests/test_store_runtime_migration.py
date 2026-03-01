from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from chordcode.store.sqlite import SQLiteStore


class StoreRuntimeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_migrates_sessions_runtime_columns_and_defaults_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.sqlite3")
            with sqlite3.connect(db_path) as db:
                db.execute(
                    """
                    CREATE TABLE sessions (
                      id TEXT PRIMARY KEY,
                      title TEXT NOT NULL,
                      worktree TEXT NOT NULL,
                      cwd TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      updated_at INTEGER NOT NULL,
                      permission_rules_json TEXT NOT NULL
                    )
                    """,
                )
                db.execute(
                    """
                    INSERT INTO sessions (id,title,worktree,cwd,created_at,updated_at,permission_rules_json)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        "s1",
                        "legacy",
                        "/tmp",
                        "/tmp",
                        1,
                        1,
                        json.dumps([{"permission": "*", "pattern": "*", "action": "allow"}]),
                    ),
                )
                db.commit()

            store = SQLiteStore(db_path)
            await store.init()

            with sqlite3.connect(db_path) as db:
                cols = [row[1] for row in db.execute("PRAGMA table_info(sessions)").fetchall()]
                self.assertIn("runtime_backend", cols)
                self.assertIn("runtime_json", cols)

            session = await store.get_session("s1")
            self.assertEqual(session.runtime.backend, "local")
            self.assertIsNone(session.runtime.daytona)


if __name__ == "__main__":
    unittest.main()
