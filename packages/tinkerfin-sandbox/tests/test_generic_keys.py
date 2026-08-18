from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxConfig,
    OpenSandboxManager,
    OpenSandboxRuntimeInfo,
)


@dataclass(frozen=True, slots=True)
class _ProjectKey:
    organization: str
    project: str


class _Backend:
    enable_capture_offload = False

    def __init__(self, sandbox_id: str) -> None:
        self.id = sandbox_id

    async def aexecute(
        self,
        _command: str,
        *,
        timeout: int | None = None,
    ) -> SimpleNamespace:
        del timeout
        return SimpleNamespace(exit_code=0)

    async def arenew(self, _timeout: timedelta) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.config = OpenSandboxConfig(
            health_command="true",
            command_timeout=30,
            ttl=timedelta(hours=1),
            warm_pool_size=0,
            workspace_root=None,
        )
        self.create_calls = 0
        self.destroyed: list[str] = []
        self.closed = False

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        del metadata
        self.create_calls += 1
        return cast(OpenSandboxBackend, _Backend(f"sandbox-{self.create_calls}"))

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        return cast(OpenSandboxBackend, _Backend(sandbox_id))

    async def inspect(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        raise AssertionError(f"unexpected inspect for {sandbox_id}")

    async def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_manager_reports_key_resolver_contract_in_english() -> None:
    client = _Client()
    with pytest.raises(TypeError, match="key_resolver must be callable"):
        OpenSandboxManager(
            client=client,
            key_resolver=cast(Callable[[str], str], None),
        )

    manager = OpenSandboxManager[str](
        client=client,
        key_resolver=lambda _key: cast(str, object()),
        warm_pool_size=0,
    )
    async with manager:
        with pytest.raises(TypeError, match="key_resolver must return a string"):
            await manager.get("opaque")


@pytest.mark.asyncio
async def test_manager_resolves_opaque_application_keys_without_core_types() -> None:
    client = _Client()
    manager = OpenSandboxManager[_ProjectKey](
        client=client,
        key_resolver=lambda key: f"{key.organization}/{key.project}",
        warm_pool_size=0,
    )

    async with manager:
        first = await manager.get(_ProjectKey("acme", "alpha"))
        repeated = await manager.get(_ProjectKey("acme", "alpha"))
        other = await manager.get(_ProjectKey("acme", "beta"))

    assert first is repeated
    assert first is not other
    assert client.create_calls == 2
    assert client.closed
