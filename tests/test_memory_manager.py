from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.config import (  # noqa: E402
    ChannelsConfig,
    Config,
    FeishuChannelConfig,
    HooksConfig,
    KBConfig,
    LangfuseConfig,
    LoggingConfig,
    MemoryConfig,
    OpenAIConfig,
    VLMConfig,
    WebSearchConfig,
)
from chordcode.memory.manager import MemoryManager, build_memory_db_path  # noqa: E402
from chordcode.memory.service import MemoryService  # noqa: E402
from chordcode.memory.types import MemoryChunk  # noqa: E402
from chordcode.model import PermissionRule, Session, SessionRuntime  # noqa: E402
from chordcode.store.sqlite import SQLiteStore  # noqa: E402


class FakeEmbeddingProvider:
    model = "fake-embedding"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectorize(text) for text in texts]

    def _vectorize(self, text: str) -> list[float]:
        lowered = text.lower()
        return [
            float("alpha" in lowered),
            float("release" in lowered),
            float("todo" in lowered or "task" in lowered),
            float(len(lowered)),
        ]


def make_config(*, db_path: str, worktree: str, sync_interval_seconds: int = 3) -> Config:
    return Config(
        openai=OpenAIConfig(base_url="http://local", api_key="k", model="m"),
        langfuse=LangfuseConfig(
            enabled=False,
            public_key="",
            secret_key="",
            base_url="https://cloud.langfuse.com",
            environment="test",
            sample_rate=1.0,
            debug=False,
        ),
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=False,
                app_id="",
                app_secret="",
                encrypt_key="",
                verification_token="",
                allow_from=[],
            )
        ),
        kb=KBConfig(backend="none", base_url="", api_key=""),
        vlm=VLMConfig(backend="none", api_url="", api_key="", poll_interval=5, timeout=1800),
        logging=LoggingConfig(level="INFO", console=False, file=False, dir="./data/logs", rotation="00:00", retention="7 days"),
        hooks=HooksConfig(debug=False),
        web_search=WebSearchConfig(tavily_api_key=""),
        system_prompt="sys",
        db_path=db_path,
        default_worktree=worktree,
        default_permission_action="ask",
        prompt_templates={},
        memory=MemoryConfig(
            enabled=True,
            embedding_base_url="http://embed.local/v1",
            embedding_api_key="embed-key",
            embedding_model="fake-embedding",
            sync_interval_seconds=sync_interval_seconds,
        ),
    )


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_detects_stale_files_before_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "memory").mkdir()
            (worktree / "memory.md").write_text("alpha project note\n", encoding="utf-8")
            archive = worktree / "memory" / "2026-03-10.md"
            archive.write_text("release checklist\n", encoding="utf-8")

            provider = FakeEmbeddingProvider()
            manager = MemoryManager(
                worktree=tmp,
                config=MemoryConfig(sync_interval_seconds=3600),
                db_path=build_memory_db_path(str(worktree / "app.sqlite3"), tmp),
                embedding_provider=provider,
            )
            await manager.init()
            await manager.sync()

            first = await manager.search(query="release checklist", max_results=3, min_score=0.01)
            first_hits = first["hits"]
            self.assertTrue(any(hit["path"] == "memory/2026-03-10.md" for hit in first_hits))

            archive.write_text("release checklist\ntodo next step\n", encoding="utf-8")
            second = await manager.search(query="todo next step", max_results=3, min_score=0.01)
            second_hits = second["hits"]
            self.assertTrue(any("todo next step" in hit["snippet"] for hit in second_hits))

    async def test_sync_reuses_embeddings_for_unchanged_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            memory_file = worktree / "memory.md"
            memory_file.write_text("seed", encoding="utf-8")

            provider = FakeEmbeddingProvider()
            manager = MemoryManager(
                worktree=tmp,
                config=MemoryConfig(sync_interval_seconds=3600),
                db_path=build_memory_db_path(str(worktree / "app.sqlite3"), tmp),
                embedding_provider=provider,
            )
            await manager.init()

            chunk_one = MemoryChunk(
                id="c1",
                path="memory.md",
                source="memory",
                start_line=1,
                end_line=1,
                hash="same",
                text="alpha stable",
            )
            chunk_two = MemoryChunk(
                id="c2",
                path="memory.md",
                source="memory",
                start_line=2,
                end_line=2,
                hash="old",
                text="release old",
            )
            with patch("chordcode.memory.manager.chunk_markdown", return_value=[chunk_one, chunk_two]):
                await manager.sync()
            self.assertEqual(provider.calls[0], ["alpha stable", "release old"])

            memory_file.write_text("seed-updated", encoding="utf-8")
            chunk_three = MemoryChunk(
                id="c3",
                path="memory.md",
                source="memory",
                start_line=2,
                end_line=2,
                hash="new",
                text="todo replacement",
            )
            with patch("chordcode.memory.manager.chunk_markdown", return_value=[chunk_one, chunk_three]):
                await manager.sync()
            self.assertEqual(provider.calls[1], ["todo replacement"])

    async def test_service_background_sync_updates_existing_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "memory.md").write_text("alpha\n", encoding="utf-8")

            db_path = str(root / "app.sqlite3")
            cfg = make_config(db_path=db_path, worktree=str(worktree), sync_interval_seconds=1)
            store = SQLiteStore(db_path)
            await store.init()
            session = Session(
                id="s1",
                title="memory",
                worktree=str(worktree),
                cwd=str(worktree),
                created_at=1,
                updated_at=1,
                permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                runtime=SessionRuntime(backend="local"),
            )
            await store.create_session(session)

            service = MemoryService(cfg=cfg, store=store, embedding_provider_factory=lambda _cfg: None)
            await service.start()
            manager = await service.get_manager(str(worktree))
            self.assertIsNotNone(manager)
            assert manager is not None
            await manager.sync()

            (worktree / "memory.md").write_text("beta background update\n", encoding="utf-8")
            await asyncio.sleep(1.2)
            result = await manager.search(query="beta background", max_results=3, min_score=0.01)
            self.assertTrue(any("beta background update" in hit["snippet"] for hit in result["hits"]))

            await service.stop()
