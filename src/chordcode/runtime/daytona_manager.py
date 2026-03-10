from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from chordcode.config import DaytonaConfig
from chordcode.model import Session, SessionRuntime, DaytonaRuntimeConfig
from chordcode.store.sqlite import SQLiteStore


class DaytonaUnavailableError(RuntimeError):
    pass


class DaytonaOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DaytonaSandboxRef:
    sandbox_id: str
    sandbox_name: str | None
    sandbox: Any


class DaytonaClientLike(Protocol):
    def get(self, sandbox_id: str) -> Any: ...
    def find_one(self, sandbox_id: str) -> Any: ...
    def create(self, req: Any | None = None) -> Any: ...


class DaytonaManager:
    def __init__(self, cfg: DaytonaConfig, store: SQLiteStore) -> None:
        self._cfg = cfg
        self._store = store
        self._client: Any | None = None
        self._rg_available: dict[str, bool] = {}
        self._rg_install_attempted: set[str] = set()

    def _build_client(self) -> Any:
        try:
            from daytona import Daytona, DaytonaConfig  # type: ignore
        except Exception as exc:
            raise DaytonaUnavailableError("daytona package is not installed") from exc

        cfg_kwargs: dict[str, Any] = {}
        if self._cfg.api_key:
            cfg_kwargs["api_key"] = self._cfg.api_key
        if self._cfg.target:
            cfg_kwargs["target"] = self._cfg.target

        config_obj: Any | None = None
        config_error: Exception | None = None
        if self._cfg.server_url:
            # Prefer api_url (newer SDK), fallback to server_url (older SDK).
            for url_key in ("api_url", "server_url"):
                try:
                    config_obj = DaytonaConfig(**{**cfg_kwargs, url_key: self._cfg.server_url})
                    break
                except TypeError as exc:
                    config_error = exc
        if config_obj is None:
            try:
                config_obj = DaytonaConfig(**cfg_kwargs)
            except Exception as exc:
                config_error = exc

        if config_obj is None:
            raise DaytonaOperationError(f"failed to initialize daytona client: {config_error}")

        try:
            return Daytona(config=config_obj)
        except Exception as exc:
            raise DaytonaOperationError(f"failed to initialize daytona client: {exc}") from exc

    def _client_or_create(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    @staticmethod
    def _sandbox_id_from_obj(obj: Any) -> str | None:
        for key in ("id", "sandbox_id"):
            value = getattr(obj, key, None)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _sandbox_name_from_obj(obj: Any) -> str | None:
        for key in ("name", "sandbox_name", "sandboxName"):
            value = getattr(obj, key, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _find_sandbox(self, sandbox_id: str) -> Any:
        client = cast(DaytonaClientLike, self._client_or_create())
        try:
            sandbox = client.get(sandbox_id)
            if sandbox:
                return sandbox
        except Exception:
            pass

        try:
            sandbox = client.find_one(sandbox_id)
            if sandbox:
                return sandbox
        except Exception:
            pass

        raise DaytonaOperationError(f"daytona sandbox not found: {sandbox_id}")

    def _create_sandbox(self) -> DaytonaSandboxRef:
        client = cast(DaytonaClientLike, self._client_or_create())
        try:
            sandbox = client.create()
        except TypeError:
            sandbox = client.create(None)
        except Exception as exc:
            raise DaytonaOperationError(f"failed to create daytona sandbox: {exc}") from exc

        sid = self._sandbox_id_from_obj(sandbox)
        if sid and sandbox:
            return DaytonaSandboxRef(
                sandbox_id=sid,
                sandbox_name=self._sandbox_name_from_obj(sandbox),
                sandbox=sandbox,
            )
        raise DaytonaOperationError("failed to create daytona sandbox: missing sandbox id")

    async def ensure_session_runtime_async(self, session: Session) -> Session:
        if session.runtime.backend != "daytona":
            return session
        if not self._cfg.api_key:
            raise DaytonaUnavailableError("daytona.api_key (or DAYTONA_API_KEY) is required for daytona runtime")

        sandbox_id = session.runtime.daytona.sandbox_id if session.runtime.daytona else None
        if sandbox_id:
            sandbox = self._find_sandbox(sandbox_id)
            sandbox_name = self._sandbox_name_from_obj(sandbox)
            current_name = session.runtime.daytona.sandbox_name if session.runtime.daytona else None
            if sandbox_name and sandbox_name != current_name:
                runtime = SessionRuntime(
                    backend="daytona",
                    daytona=DaytonaRuntimeConfig(sandbox_id=sandbox_id, sandbox_name=sandbox_name),
                )
                return await self._store.update_session_runtime(session.id, runtime.backend, runtime.model_dump())
            return session

        created = self._create_sandbox()
        runtime = SessionRuntime(
            backend="daytona",
            daytona=DaytonaRuntimeConfig(sandbox_id=created.sandbox_id, sandbox_name=created.sandbox_name),
        )
        return await self._store.update_session_runtime(session.id, runtime.backend, runtime.model_dump())

    async def get_sandbox_for_session(self, session: Session) -> DaytonaSandboxRef:
        ensured = await self.ensure_session_runtime_async(session)
        sandbox_id = ensured.runtime.daytona.sandbox_id if ensured.runtime.daytona else None
        if not sandbox_id:
            raise DaytonaOperationError("daytona runtime missing sandbox_id")
        sandbox = self._find_sandbox(sandbox_id)
        sandbox_name = self._sandbox_name_from_obj(sandbox)
        return DaytonaSandboxRef(sandbox_id=sandbox_id, sandbox_name=sandbox_name, sandbox=sandbox)

    def get_cached_rg_available(self, sandbox_id: str) -> bool | None:
        return self._rg_available.get(sandbox_id)

    def set_cached_rg_available(self, sandbox_id: str, available: bool) -> None:
        self._rg_available[sandbox_id] = available

    def rg_install_attempted(self, sandbox_id: str) -> bool:
        return sandbox_id in self._rg_install_attempted

    def mark_rg_install_attempted(self, sandbox_id: str) -> None:
        self._rg_install_attempted.add(sandbox_id)
