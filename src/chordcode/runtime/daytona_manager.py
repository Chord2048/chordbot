from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    sandbox: Any


class DaytonaManager:
    def __init__(self, cfg: DaytonaConfig, store: SQLiteStore) -> None:
        self._cfg = cfg
        self._store = store
        self._client: Any | None = None
        self._rg_available: dict[str, bool] = {}
        self._rg_install_attempted: set[str] = set()

    def _build_client(self) -> Any:
        try:
            from daytona import Daytona  # type: ignore
        except Exception as exc:
            raise DaytonaUnavailableError("daytona package is not installed") from exc

        kwargs: dict[str, Any] = {}
        if self._cfg.api_key:
            kwargs["api_key"] = self._cfg.api_key
        if self._cfg.server_url:
            kwargs["server_url"] = self._cfg.server_url
        if self._cfg.target:
            kwargs["target"] = self._cfg.target

        try:
            return Daytona(**kwargs)
        except TypeError:
            try:
                return Daytona()
            except Exception as exc:
                raise DaytonaOperationError(f"failed to initialize daytona client: {exc}") from exc
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

    def _find_sandbox(self, sandbox_id: str) -> Any:
        client = self._client_or_create()

        getter = getattr(client, "get", None)
        if callable(getter):
            try:
                sandbox = getter(sandbox_id)
                if sandbox:
                    return sandbox
            except Exception:
                pass

        find_one = getattr(client, "find_one", None)
        if callable(find_one):
            for params in (
                {"id": sandbox_id},
                {"sandbox_id": sandbox_id},
                {"sandboxId": sandbox_id},
            ):
                try:
                    sandbox = find_one(params)
                    if sandbox:
                        return sandbox
                except Exception:
                    continue

        raise DaytonaOperationError(f"daytona sandbox not found: {sandbox_id}")

    def _create_sandbox(self) -> DaytonaSandboxRef:
        client = self._client_or_create()
        create = getattr(client, "create", None)
        if callable(create):
            for payload in (None, {}):
                try:
                    sandbox = create() if payload is None else create(payload)
                    sid = self._sandbox_id_from_obj(sandbox)
                    if sid and sandbox:
                        return DaytonaSandboxRef(sandbox_id=sid, sandbox=sandbox)
                except TypeError:
                    continue
                except Exception as exc:
                    raise DaytonaOperationError(f"failed to create daytona sandbox: {exc}") from exc

        create_sandbox = getattr(client, "create_sandbox", None)
        if callable(create_sandbox):
            try:
                sandbox = create_sandbox()
                sid = self._sandbox_id_from_obj(sandbox)
                if sid and sandbox:
                    return DaytonaSandboxRef(sandbox_id=sid, sandbox=sandbox)
            except Exception as exc:
                raise DaytonaOperationError(f"failed to create daytona sandbox: {exc}") from exc

        raise DaytonaOperationError("daytona client does not expose sandbox create APIs")

    async def ensure_session_runtime_async(self, session: Session) -> Session:
        if session.runtime.backend != "daytona":
            return session
        if not self._cfg.api_key:
            raise DaytonaUnavailableError("daytona.api_key (or DAYTONA_API_KEY) is required for daytona runtime")

        sandbox_id = session.runtime.daytona.sandbox_id if session.runtime.daytona else None
        if sandbox_id:
            self._find_sandbox(sandbox_id)
            return session

        created = self._create_sandbox()
        runtime = SessionRuntime(
            backend="daytona",
            daytona=DaytonaRuntimeConfig(sandbox_id=created.sandbox_id),
        )
        return await self._store.update_session_runtime(session.id, runtime.backend, runtime.model_dump())

    async def get_sandbox_for_session(self, session: Session) -> DaytonaSandboxRef:
        ensured = await self.ensure_session_runtime_async(session)
        sandbox_id = ensured.runtime.daytona.sandbox_id if ensured.runtime.daytona else None
        if not sandbox_id:
            raise DaytonaOperationError("daytona runtime missing sandbox_id")
        sandbox = self._find_sandbox(sandbox_id)
        return DaytonaSandboxRef(sandbox_id=sandbox_id, sandbox=sandbox)

    def get_cached_rg_available(self, sandbox_id: str) -> bool | None:
        return self._rg_available.get(sandbox_id)

    def set_cached_rg_available(self, sandbox_id: str, available: bool) -> None:
        self._rg_available[sandbox_id] = available

    def rg_install_attempted(self, sandbox_id: str) -> bool:
        return sandbox_id in self._rg_install_attempted

    def mark_rg_install_attempted(self, sandbox_id: str) -> None:
        self._rg_install_attempted.add(sandbox_id)
