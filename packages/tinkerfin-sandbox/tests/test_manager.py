from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol, ExecuteResponse
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore
from opensandbox.config import ConnectionConfig

import tinkerfin_sandbox
from tinkerfin_sandbox import (
    InMemoryOpenSandboxState,
    OpenSandboxBackend,
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxDestroyError,
    OpenSandboxDetails,
    OpenSandboxHandle,
    OpenSandboxHandleOwnershipError,
    OpenSandboxManager,
    OpenSandboxManagerClosedError,
    OpenSandboxOwnerClaim,
    OpenSandboxPlatformInfo,
    OpenSandboxResetError,
    OpenSandboxRuntimeInfo,
    OpenSandboxState,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
    OpenSandboxStatusInfo,
    OpenSandboxUnavailableReason,
    OpenSandboxWarmClaim,
    RootedOpenSandboxBackend,
    SQLAlchemyOpenSandboxState,
)
from tinkerfin_sandbox.backends import _rooted_protocol


def _key(value: str) -> str:
    return value


def _new_manager(**kwargs: Any) -> OpenSandboxManager[str]:
    return OpenSandboxManager(key_resolver=_key, **kwargs)


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """Allow deterministic responses to invoke Deep Agents tools."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _ExecuteResponse:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _FakeBackend:
    def __init__(
        self,
        sandbox_id: str,
        *,
        healthy: bool = True,
        runtime_info: OpenSandboxRuntimeInfo | None = None,
    ) -> None:
        self.id = sandbox_id
        self.healthy = healthy
        self.runtime_info = runtime_info or _runtime_info(sandbox_id)
        self.execute_calls: list[tuple[str, int | None]] = []
        self.renew_calls: list[timedelta] = []
        self.close_calls = 0
        self.kill_calls = 0
        self.close_entered = threading.Event()
        self.close_gate: threading.Event | None = None
        self.runtime_info_calls = 0
        self.block_command: str | None = None
        self.execute_entered = threading.Event()
        self.execute_gate = threading.Event()

    def execute(self, command: str, *, timeout: int | None = None) -> _ExecuteResponse:
        self.execute_calls.append((command, timeout))
        if command == self.block_command:
            self.execute_entered.set()
            if not self.execute_gate.wait(timeout=1):
                raise TimeoutError("test execution gate was not released")
        return _ExecuteResponse(0 if self.healthy else 1)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> _ExecuteResponse:
        self.execute_calls.append((command, timeout))
        if command == self.block_command:
            self.execute_entered.set()
            released = await asyncio.to_thread(self.execute_gate.wait, 1)
            if not released:
                raise TimeoutError("test execution gate was not released")
        return _ExecuteResponse(0 if self.healthy else 1)

    def renew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.close_gate is not None and not self.close_gate.wait(timeout=1):
            raise TimeoutError("test close gate was not released")

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    async def akill(self) -> None:
        self.kill_calls += 1

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        self.runtime_info_calls += 1
        return self.runtime_info

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        self.runtime_info_calls += 1
        return self.runtime_info


class _FakeClient:
    def __init__(
        self,
        backend_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = OpenSandboxConfig(
            health_command="echo ok",
            command_timeout=30,
            ttl=timedelta(hours=2),
            warm_pool_size=0,
            workspace_root=None,
        )
        self._backend_factory = backend_factory or _FakeBackend
        self.backends: list[Any] = []
        self.connected: dict[str, Any] = {}
        self.inspection_results: dict[str, OpenSandboxRuntimeInfo] = {}
        self.create_calls = 0
        self.create_metadata: list[dict[str, str]] = []
        self.connect_calls: list[str] = []
        self.inspect_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.destroy_errors: dict[str, Exception] = {}
        self.destroy_entered = asyncio.Event()
        self.destroy_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.create_gate: asyncio.Event | None = None
        self.release_after_create_count: int | None = None
        self.close_calls = 0

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        create_number = self.create_calls
        self.create_entered.set()
        if (
            self.release_after_create_count is not None
            and self.create_calls >= self.release_after_create_count
            and self.create_gate is not None
        ):
            self.create_gate.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        backend = self._backend_factory(f"sandbox-{create_number}")
        self.backends.append(backend)
        return cast(OpenSandboxBackend, backend)

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.connect_calls.append(sandbox_id)
        return cast(OpenSandboxBackend, self.connected[sandbox_id])

    async def inspect(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        self.inspect_calls.append(sandbox_id)
        return self.inspection_results[sandbox_id]

    async def destroy(self, sandbox_id: str) -> None:
        self.destroy_calls.append(sandbox_id)
        self.destroy_entered.set()
        if self.destroy_gate is not None:
            await self.destroy_gate.wait()
        error = self.destroy_errors.get(sandbox_id)
        if error is not None:
            raise error

    async def aclose(self) -> None:
        self.close_calls += 1


class _FakeState(InMemoryOpenSandboxState):
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        super().__init__()
        self.bindings = dict(bindings or {})
        self.binding_generations = {owner_key: 0 for owner_key in self.bindings}
        self.get_calls: list[str] = []
        self.save_calls: list[tuple[str, str]] = []
        self.consume_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.get_error: Exception | None = None
        self.read_error: Exception | None = None
        self.read_override_enabled = False
        self.read_override: OpenSandboxBinding | None = None
        self.save_error: Exception | None = None
        self.commit_before_save_error = False
        self.cancel_after_save_commit = False
        self.delete_error: Exception | None = None

    @property
    def persistent(self) -> bool:
        return True

    async def acquire_owner(self, owner_key: str):
        self.get_calls.append(owner_key)
        if self.get_error is not None:
            raise OpenSandboxStateError("fake state read failed") from self.get_error
        claim = await super().acquire_owner(owner_key)
        desired_id = self.bindings.get(owner_key)
        current_id = claim.binding.sandbox_id if claim.binding is not None else None
        if desired_id == current_id:
            return claim
        if desired_id is None:
            await super().unbind_owner(claim)
            self.binding_generations.pop(owner_key, None)
            binding = None
        else:
            binding = await super().bind_owner(claim, desired_id)
            self.binding_generations[owner_key] = binding.generation
        return replace(claim, binding=binding)

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        self.get_calls.append(owner_key)
        read_error = self.read_error or self.get_error
        if read_error is not None:
            raise OpenSandboxStateError("fake binding reconciliation failed") from (
                read_error
            )
        if self.read_override_enabled:
            return self.read_override
        sandbox_id = self.bindings.get(owner_key)
        if sandbox_id is None:
            return None
        return OpenSandboxBinding(
            sandbox_id=sandbox_id,
            generation=self.binding_generations.get(owner_key, 0),
        )

    async def bind_owner(self, claim, sandbox_id: str) -> OpenSandboxBinding:
        self.save_calls.append((claim.owner_key, sandbox_id))
        if self.commit_before_save_error or self.cancel_after_save_commit:
            binding = await super().bind_owner(claim, sandbox_id)
            self.bindings[claim.owner_key] = sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
        if self.cancel_after_save_commit:
            raise asyncio.CancelledError("bind response was cancelled after commit")
        if self.save_error is not None:
            raise OpenSandboxStateError("fake state write failed") from self.save_error
        if not self.commit_before_save_error:
            binding = await super().bind_owner(claim, sandbox_id)
            self.bindings[claim.owner_key] = sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
        return binding

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        binding = await super().consume_warm(claim)
        if binding is not None:
            self.consume_calls.append((claim.owner_key, binding.sandbox_id))
            self.bindings[claim.owner_key] = binding.sandbox_id
            self.binding_generations[claim.owner_key] = binding.generation
        return binding

    async def unbind_owner(self, claim) -> None:
        self.delete_calls.append(claim.owner_key)
        if self.delete_error is not None:
            raise OpenSandboxStateError(
                "fake state delete failed"
            ) from self.delete_error
        await super().unbind_owner(claim)
        self.bindings.pop(claim.owner_key, None)
        self.binding_generations.pop(claim.owner_key, None)

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        return ()


class _CleanupObservedSQLState(SQLAlchemyOpenSandboxState):
    """暴露真实 cleanup 提交完成点，避免测试依赖事件循环时序"""

    def __init__(self, *, url: str, namespace: str) -> None:
        super().__init__(url=url, namespace=namespace)
        self.cleanup_completed = asyncio.Event()

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        await super().complete_cleanup(claim)
        self.cleanup_completed.set()


class _BlockingOwnerReleaseState(_FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()
        self.release_cancelled = asyncio.Event()
        self.claim: OpenSandboxOwnerClaim | None = None

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        self.claim = claim
        self.release_started.set()
        try:
            await self.release_gate.wait()
        except asyncio.CancelledError:
            self.release_cancelled.set()
            raise
        await super().release_owner(claim)


class _BlockingWarmReleaseState(_FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()
        self.release_cancelled = asyncio.Event()
        self.claim: OpenSandboxWarmClaim | None = None
        self.close_calls = 0

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        self.claim = claim
        self.release_started.set()
        try:
            await self.release_gate.wait()
        except asyncio.CancelledError:
            self.release_cancelled.set()
            raise
        await super().release_warm(claim)

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


class _ObservedWarmCreateClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.current_create_task: asyncio.Task[object] | None = None

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - async methods run in a Task
            raise RuntimeError("warm creation requires an asyncio task")
        self.current_create_task = cast(asyncio.Task[object], current)
        return await super().create(metadata=metadata)


class _WarmLeaseState(OpenSandboxState):
    """Delegate real memory State behavior while exercising renewable claims."""

    def __init__(self, *, renews: bool) -> None:
        self._inner = InMemoryOpenSandboxState()
        self._renews = renews

    @property
    def persistent(self) -> bool:
        return self._inner.persistent

    @property
    def lease_renew_interval(self) -> float | None:
        return 0.01

    async def start(self, *, warm_pool_size: int) -> None:
        await self._inner.start(warm_pool_size=warm_pool_size)

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        return await self._inner.acquire_owner(owner_key)

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        return await self._inner.renew_owner(claim)

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
    ) -> OpenSandboxBinding:
        return await self._inner.bind_owner(claim, sandbox_id)

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        await self._inner.unbind_owner(claim)

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        return await self._inner.read_binding(owner_key)

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        await self._inner.release_owner(claim)

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        return await self._inner.claim_warm_slot()

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        await self._inner.publish_warm(claim, sandbox_id)

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        if not self._renews:
            return False
        return await self._inner.renew_warm(claim)

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        await self._inner.release_warm(claim)

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        return await self._inner.consume_warm(claim)

    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        await self._inner.enqueue_cleanup(sandbox_id)

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        return await self._inner.claim_cleanup()

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        return await self._inner.renew_cleanup(claim)

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        await self._inner.complete_cleanup(claim)

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        await self._inner.release_cleanup(claim)

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        return await self._inner.shutdown_sandbox_ids()

    async def aclose(self) -> None:
        await self._inner.aclose()


class _WarmBindingLossState(_WarmLeaseState):
    """Lose the owner lease only after a warm Sandbox becomes authoritative."""

    def __init__(self) -> None:
        super().__init__(renews=True)
        self.consume_calls: list[tuple[str, str]] = []
        self.bindings: dict[str, str] = {}

    @property
    def persistent(self) -> bool:
        return True

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        if self.consume_calls:
            return False
        return await super().renew_owner(claim)

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        binding = await super().consume_warm(claim)
        if binding is not None:
            self.consume_calls.append((claim.owner_key, binding.sandbox_id))
            self.bindings[claim.owner_key] = binding.sandbox_id
        return binding

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        return ()


class _RepeatedCancellationWarmClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.first_cancellation = asyncio.Event()
        self.repeated_cancellation = asyncio.Event()
        self.release_create = asyncio.Event()
        self.create_returned = asyncio.Event()

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        self.create_entered.set()
        while not self.release_create.is_set():
            try:
                await self.release_create.wait()
            except asyncio.CancelledError:
                if self.first_cancellation.is_set():
                    self.repeated_cancellation.set()
                else:
                    self.first_cancellation.set()
        backend = _FakeBackend(f"sandbox-{self.create_calls}")
        self.backends.append(backend)
        self.create_returned.set()
        return cast(OpenSandboxBackend, backend)


class _NativeOwnershipClient(OpenSandboxClient):
    def __init__(self, backend: _FakeBackend) -> None:
        super().__init__(
            connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
            config=OpenSandboxConfig(
                warm_pool_size=0,
                workspace_root=None,
            ),
        )
        self.backend = backend
        self.create_entered = asyncio.Event()
        self.release_create = asyncio.Event()
        self.destroy_calls: list[str] = []

    async def _create(
        self,
        metadata: Mapping[str, str] | None,
    ) -> OpenSandboxBackend:
        del metadata
        self.create_entered.set()
        await self.release_create.wait()
        return cast(OpenSandboxBackend, self.backend)

    async def destroy(self, sandbox_id: str) -> None:
        self.destroy_calls.append(sandbox_id)


class _CancelledWarmupClient(_FakeClient):
    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        raise asyncio.CancelledError("warm creation cancelled")


class _CloseObservedState(_FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


def _runtime_info(
    sandbox_id: str,
    *,
    available: bool = True,
    healthy: bool = True,
    unavailable_reason: OpenSandboxUnavailableReason | None = None,
) -> OpenSandboxRuntimeInfo:
    return OpenSandboxRuntimeInfo(
        sandbox_id=sandbox_id,
        available=available,
        healthy=healthy,
        status=(OpenSandboxStatusInfo(state="RUNNING") if available else None),
        created_at=datetime(2026, 8, 8, 1, 2, tzinfo=UTC),
        expires_at=datetime(2026, 8, 8, 3, 2, tzinfo=UTC),
        image="registry.example/sandbox:1",
        platform=OpenSandboxPlatformInfo(os="linux", arch="amd64"),
        metadata={"region": "local"},
        unavailable_reason=unavailable_reason,
    )


async def _eventually(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class _NotificationLoop:
    def __init__(self, *, close_during_notification: bool) -> None:
        self._closed = False
        self.close_during_notification = close_during_notification

    def is_closed(self) -> bool:
        return self._closed

    def call_soon_threadsafe(
        self,
        callback: Callable[..., object],
        *args: object,
    ) -> None:
        del callback, args
        if self.close_during_notification:
            self._closed = True
        raise RuntimeError("test loop rejected notification")


def _append_idle_waiter(
    handle: OpenSandboxHandle,
    backend: _FakeBackend,
    loop: asyncio.AbstractEventLoop,
    waiter: asyncio.Future[None],
) -> None:
    with handle._condition:
        handle._idle_waiters.setdefault(id(backend), []).append((loop, waiter))


def test_closed_waiter_loop_does_not_replace_lease_body_failure() -> None:
    backend = _FakeBackend("sandbox-1")
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, backend))
    loop = asyncio.new_event_loop()
    waiter = loop.create_future()
    body_error = ValueError("lease body failed")

    try:
        with pytest.raises(ValueError) as captured:
            with handle._lease() as leased:
                assert leased is backend
                _append_idle_waiter(handle, backend, loop, waiter)
                loop.close()
                raise body_error
    finally:
        if not loop.is_closed():
            loop.close()

    assert captured.value is body_error


@pytest.mark.parametrize("close_during_notification", (False, True))
async def test_waiter_notification_suppresses_only_confirmed_loop_close(
    close_during_notification: bool,
) -> None:
    backend = _FakeBackend("sandbox-1")
    handle = OpenSandboxHandle(cast(OpenSandboxBackend, backend))
    loop = _NotificationLoop(
        close_during_notification=close_during_notification,
    )
    waiter = asyncio.get_running_loop().create_future()

    if close_during_notification:
        with handle._lease():
            _append_idle_waiter(
                handle,
                backend,
                cast(asyncio.AbstractEventLoop, loop),
                waiter,
            )
    else:
        with pytest.raises(RuntimeError, match="rejected notification"):
            with handle._lease():
                _append_idle_waiter(
                    handle,
                    backend,
                    cast(asyncio.AbstractEventLoop, loop),
                    waiter,
                )


class OpenSandboxManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.managers: list[OpenSandboxManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.aclose()

    async def _manager(
        self,
        client: _FakeClient,
        store: _FakeState | None = None,
        *,
        warm_pool_size: int = 0,
    ) -> OpenSandboxManager:
        manager = _new_manager(
            client=client,
            state=store,
            warm_pool_size=warm_pool_size,
        )
        self.managers.append(manager)
        await manager.start()
        return manager

    async def test_same_owner_concurrent_get_creates_only_one_sandbox(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        store = _FakeState()
        manager = await self._manager(client, store)

        first = asyncio.create_task(manager.get(_key("user-1")))
        second = asyncio.create_task(manager.get(_key("user-1")))
        await client.create_entered.wait()
        await asyncio.sleep(0)

        self.assertEqual(client.create_calls, 1)
        client.create_gate.set()
        first_handle, second_handle = await asyncio.gather(first, second)

        self.assertIs(first_handle, second_handle)
        self.assertEqual(store.save_calls, [("user-1", "sandbox-1")])

    async def test_different_owners_can_create_sandboxes_concurrently(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        client.release_after_create_count = 2
        manager = await self._manager(client, _FakeState())

        first_handle, second_handle = await asyncio.wait_for(
            asyncio.gather(manager.get(_key("user-1")), manager.get(_key("user-2"))),
            timeout=1,
        )

        self.assertEqual(client.create_calls, 2)
        self.assertNotEqual(first_handle.id, second_handle.id)

    async def test_key_resolver_can_share_one_sandbox_across_inputs(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = OpenSandboxManager(
            client=client,
            key_resolver=lambda _key: "global",
            state=store,
            warm_pool_size=0,
        )
        self.managers.append(manager)
        await manager.start()

        first, other = await asyncio.gather(
            manager.get("request-a"),
            manager.get("request-b"),
        )

        self.assertIs(first, other)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(store.save_calls, [("global", "sandbox-1")])

    async def test_resolved_keys_separate_independent_sandbox_owners(
        self,
    ) -> None:
        client = _FakeClient()
        manager = _new_manager(client=client, warm_pool_size=0)
        self.managers.append(manager)
        await manager.start()

        first, repeated, other = await asyncio.gather(
            manager.get(_key("user-1/agent-a")),
            manager.get(_key("user-1/agent-a")),
            manager.get(_key("user-1/agent-b")),
        )

        self.assertIs(first, repeated)
        self.assertIsNot(first, other)
        self.assertEqual(client.create_calls, 2)

    async def test_store_recovery_takes_priority_over_warm_sandbox(self) -> None:
        client = _FakeClient()
        restored = _FakeBackend("persisted-id")
        client.connected[restored.id] = restored
        store = _FakeState({"user-1": restored.id})
        manager = await self._manager(client, store, warm_pool_size=1)

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, restored.id)
        self.assertEqual(client.connect_calls, [restored.id])
        self.assertEqual(client.create_calls, 1, "only the warm reserve is created")
        self.assertEqual(store.save_calls, [])

    async def test_get_claims_warm_sandbox_and_replenishes_pool(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)

        handle = await manager.get(_key("user-1"))
        await _eventually(lambda: client.create_calls == 2)

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(store.consume_calls, [("user-1", "sandbox-1")])
        self.assertEqual(store.save_calls, [])
        self.assertEqual(store.bindings, {"user-1": "sandbox-1"})
        self.assertEqual(
            [backend.id for backend in client.backends],
            [
                "sandbox-1",
                "sandbox-2",
            ],
        )

    async def test_unhealthy_warm_sandbox_is_destroyed_before_binding(self) -> None:
        def backend_factory(sandbox_id: str) -> _FakeBackend:
            return _FakeBackend(sandbox_id, healthy=sandbox_id != "sandbox-1")

        client = _FakeClient(backend_factory)
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)

        handle = await manager.get(_key("user-1"))

        self.assertNotEqual(handle.id, "sandbox-1")
        self.assertEqual(client.backends[0].execute_calls, [("echo ok", None)])
        self.assertIn("sandbox-1", client.destroy_calls)
        self.assertEqual(client.backends[0].close_calls, 1)
        self.assertNotIn(("user-1", "sandbox-1"), store.save_calls)

    async def test_cached_healthy_sandbox_is_reused_and_renewed(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, _FakeState())
        first_handle = await manager.get(_key("user-1"))
        backend = client.backends[0]
        health_count = len(backend.execute_calls)
        renew_count = len(backend.renew_calls)

        second_handle = await manager.get(_key("user-1"))

        self.assertIs(second_handle, first_handle)
        self.assertEqual(len(backend.execute_calls), health_count + 1)
        self.assertEqual(len(backend.renew_calls), renew_count + 1)
        self.assertEqual(client.create_calls, 1)

    async def test_is_healthy_never_creates_or_renews_sandbox(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, _FakeState())

        self.assertFalse(await manager.is_healthy(_key("user-1")))
        self.assertEqual(client.create_calls, 0)

        await manager.get(_key("user-1"))
        backend = client.backends[0]
        renew_count = len(backend.renew_calls)
        health_count = len(backend.execute_calls)

        self.assertTrue(await manager.is_healthy(_key("user-1")))
        self.assertEqual(len(backend.renew_calls), renew_count)
        self.assertEqual(len(backend.execute_calls), health_count + 1)
        self.assertEqual(client.create_calls, 1)

    async def test_recreate_hot_swaps_existing_open_handle(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))

        recreated = await manager.recreate(_key("user-1"))

        self.assertIs(recreated, handle)
        self.assertEqual(recreated.id, "sandbox-2")
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(store.bindings, {"user-1": "sandbox-2"})

    async def test_unhealthy_persisted_binding_is_replaced(self) -> None:
        restored = _FakeBackend("persisted-id", healthy=False)
        client = _FakeClient()
        client.connected[restored.id] = restored
        store = _FakeState({"user-1": restored.id})
        manager = await self._manager(client, store)

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(client.connect_calls, [restored.id])
        self.assertEqual(restored.close_calls, 1)
        self.assertEqual(client.destroy_calls, [restored.id])
        self.assertEqual(store.bindings, {"user-1": handle.id})

    async def test_failed_persisted_reconnect_is_replaced(self) -> None:
        client = _FakeClient()
        store = _FakeState({"user-1": "missing-id"})
        manager = await self._manager(client, store)

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(client.connect_calls, ["missing-id"])
        self.assertEqual(client.destroy_calls, ["missing-id"])
        self.assertEqual(store.bindings, {"user-1": handle.id})

    async def test_unhealthy_sandbox_is_hot_replaced_on_stable_handle(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.healthy = False

        replacement_handle = await manager.get(_key("user-1"))

        self.assertIs(replacement_handle, handle)
        self.assertEqual(handle.id, "sandbox-2")
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(old_backend.close_calls, 1)
        self.assertEqual(
            store.save_calls,
            [("user-1", "sandbox-1"), ("user-1", "sandbox-2")],
        )

    async def test_hot_replacement_waits_for_in_flight_backend_call(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, _FakeState())
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.block_command = "long-running"

        execute_task = asyncio.create_task(
            asyncio.to_thread(handle.execute, "long-running")
        )
        entered = await asyncio.to_thread(old_backend.execute_entered.wait, 1)
        self.assertTrue(entered)
        old_backend.healthy = False
        replace_task = asyncio.create_task(manager.get(_key("user-1")))
        await _eventually(lambda: handle.id == "sandbox-2")

        self.assertFalse(replace_task.done())
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(old_backend.close_calls, 0)
        with self.assertRaises(OpenSandboxHandleOwnershipError):
            handle.close()
        self.assertEqual(client.backends[1].close_calls, 0)

        old_backend.execute_gate.set()
        await execute_task
        replaced = await replace_task

        self.assertIs(replaced, handle)
        self.assertFalse(handle.is_closed)
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(old_backend.close_calls, 1)
        self.assertEqual(client.backends[1].close_calls, 0)

    async def test_cancelled_hot_replacement_tracks_old_backend_cleanup(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.block_command = "long-running"

        execute_task = asyncio.create_task(
            asyncio.to_thread(handle.execute, "long-running")
        )
        entered = await asyncio.to_thread(old_backend.execute_entered.wait, 1)
        self.assertTrue(entered)
        old_backend.healthy = False
        replace_task = asyncio.create_task(manager.get(_key("user-1")))
        await _eventually(lambda: handle.id == "sandbox-2")

        replace_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await replace_task
        old_backend.execute_gate.set()
        await execute_task

        # 模拟 task 已结束但 done callback 尚未从跟踪集合移除的调度窗口
        stale_cleanup_task = asyncio.create_task(asyncio.sleep(0))
        await stale_cleanup_task
        manager._cleanup_tasks.add(stale_cleanup_task)
        await manager.aclose()

        self.assertEqual(store.bindings, {"user-1": "sandbox-2"})
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(old_backend.close_calls, 1)
        self.assertEqual(client.backends[1].close_calls, 1)

    async def test_binding_failure_rolls_back_new_sandbox(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        store.save_error = RuntimeError("database unavailable")
        manager = await self._manager(client, store)

        with self.assertRaises(OpenSandboxStateError) as context:
            await manager.get(_key("user-1"))

        self.assertIsInstance(context.exception.__cause__, RuntimeError)
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(client.backends[0].close_calls, 1)
        store.save_error = None
        handle = await manager.get(_key("user-1"))
        self.assertEqual(handle.id, "sandbox-2")

    async def test_committed_warm_binding_skips_redundant_bind(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        store.commit_before_save_error = True
        store.save_error = RuntimeError("response lost after commit")

        handle = await manager.get(_key("user-1"))

        self.assertEqual(handle.id, "sandbox-1")
        self.assertEqual(store.consume_calls, [("user-1", "sandbox-1")])
        self.assertEqual(store.save_calls, [])
        self.assertEqual(store.bindings, {"user-1": "sandbox-1"})
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(client.backends[0].close_calls, 0)

    async def test_manager_owned_handle_rejects_public_close(
        self,
    ) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))

        with self.assertRaises(OpenSandboxHandleOwnershipError):
            handle.close()
        same_handle = await manager.get(_key("user-1"))

        self.assertIs(same_handle, handle)
        self.assertEqual(store.bindings, {"user-1": "sandbox-1"})
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(client.backends[0].close_calls, 0)

    async def test_delete_destroys_sandbox_and_removes_binding(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        await manager.get(_key("user-1"))
        backend = client.backends[0]

        await manager.delete(_key("user-1"))

        self.assertEqual(client.destroy_calls, [backend.id])
        self.assertEqual(backend.close_calls, 1)
        self.assertEqual(store.delete_calls, ["user-1"])
        self.assertNotIn("user-1", store.bindings)

    async def test_delete_uses_store_binding_without_connecting(self) -> None:
        client = _FakeClient()
        store = _FakeState({"user-1": "persisted-id"})
        manager = await self._manager(client, store)

        await manager.delete(_key("user-1"))

        self.assertEqual(client.destroy_calls, ["persisted-id"])
        self.assertEqual(client.connect_calls, [])
        self.assertEqual(store.delete_calls, ["user-1"])

    async def test_delete_failure_keeps_binding_and_can_be_retried(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)
        handle = await manager.get(_key("user-1"))
        client.destroy_errors[handle.id] = RuntimeError("kill failed")

        with self.assertRaises(OpenSandboxDestroyError) as context:
            await manager.delete(_key("user-1"))

        self.assertIsInstance(context.exception.__cause__, RuntimeError)
        self.assertEqual(store.bindings, {"user-1": handle.id})
        self.assertEqual(store.delete_calls, [])
        self.assertEqual(client.backends[0].close_calls, 1)

        client.destroy_errors.clear()
        await manager.delete(_key("user-1"))
        self.assertEqual(store.bindings, {})
        self.assertEqual(client.destroy_calls, [handle.id, handle.id])

    async def test_delete_failure_without_store_retains_retry_target(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client)
        handle = await manager.get(_key("user-1"))
        client.destroy_errors[handle.id] = RuntimeError("kill failed")

        with self.assertRaises(OpenSandboxDestroyError):
            await manager.delete(_key("user-1"))

        client.destroy_errors.clear()
        await manager.delete(_key("user-1"))
        self.assertEqual(client.destroy_calls, [handle.id, handle.id])

    async def test_cancelled_delete_retains_retry_target_without_store(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client)
        handle = await manager.get(_key("user-1"))
        client.destroy_gate = asyncio.Event()
        client.destroy_errors[handle.id] = RuntimeError("kill failed")

        delete_task = asyncio.create_task(manager.delete(_key("user-1")))
        await client.destroy_entered.wait()
        delete_task.cancel()
        client.destroy_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await delete_task

        client.destroy_gate = None
        client.destroy_errors.clear()
        await manager.delete(_key("user-1"))
        self.assertEqual(client.destroy_calls, [handle.id, handle.id])

    async def test_concurrent_start_and_close_share_startup_completion(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        manager = _new_manager(client=client, warm_pool_size=1)
        self.managers.append(manager)

        first_start = asyncio.create_task(manager.start())
        await client.create_entered.wait()
        second_start = asyncio.create_task(manager.start())
        close_task = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        second_finished_early = second_start.done()
        close_finished_early = close_task.done()

        client.create_gate.set()
        await asyncio.gather(first_start, second_start, close_task)

        self.assertFalse(second_finished_early)
        self.assertFalse(close_finished_early)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.destroy_calls, ["sandbox-1"])
        self.assertEqual(client.backends[0].close_calls, 1)

    async def test_aclose_preserves_persistent_warm_and_bound_remote_sandboxes(
        self,
    ) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        handle = await manager.get(_key("user-1"))
        await _eventually(lambda: client.create_calls == 2)
        bound_backend, warm_backend = client.backends

        await manager.aclose()
        await manager.aclose()

        self.assertEqual(handle.id, bound_backend.id)
        self.assertEqual(bound_backend.close_calls, 1)
        self.assertNotIn(bound_backend.id, client.destroy_calls)
        self.assertEqual(warm_backend.close_calls, 1)
        self.assertNotIn(warm_backend.id, client.destroy_calls)
        self.assertEqual(store.bindings, {"user-1": bound_backend.id})
        self.assertEqual(store.delete_calls, [])

    async def test_aclose_waits_for_in_flight_get_and_closes_its_backend(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        store = _FakeState()
        manager = await self._manager(client, store)

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        await client.create_entered.wait()
        close_task = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)

        self.assertFalse(close_task.done())
        client.create_gate.set()
        handle = await get_task
        await close_task

        self.assertEqual(store.bindings, {"user-1": handle.id})
        self.assertEqual(client.backends[0].close_calls, 1)
        self.assertEqual(client.destroy_calls, [])

    async def test_concurrent_aclose_callers_wait_for_the_same_cleanup(self) -> None:
        client = _FakeClient()
        client.create_gate = asyncio.Event()
        manager = await self._manager(client, _FakeState())

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        await client.create_entered.wait()
        first_close = asyncio.create_task(manager.aclose())
        second_close = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)

        self.assertFalse(first_close.done())
        self.assertFalse(second_close.done())
        client.create_gate.set()
        await get_task
        await asyncio.gather(first_close, second_close)
        self.assertEqual(client.backends[0].close_calls, 1)

    async def test_cancelled_aclose_caller_does_not_cancel_shared_cleanup(self) -> None:
        client = _FakeClient()
        manager = await self._manager(client, warm_pool_size=1)
        backend = client.backends[0]
        backend.close_gate = threading.Event()

        first_close = asyncio.create_task(manager.aclose())
        entered = await asyncio.to_thread(backend.close_entered.wait, 1)
        self.assertTrue(entered)
        first_close.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_close

        second_close = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        second_finished_early = second_close.done()
        backend.close_gate.set()
        await second_close

        self.assertFalse(second_finished_early)
        self.assertEqual(client.destroy_calls, [backend.id])
        self.assertEqual(backend.close_calls, 1)

    async def test_cancelled_committed_warm_health_check_closes_locally(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store, warm_pool_size=1)
        backend = client.backends[0]
        backend.block_command = "echo ok"

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        entered = await asyncio.to_thread(backend.execute_entered.wait, 1)
        self.assertTrue(entered)
        get_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await get_task
        backend.execute_gate.set()

        await manager.aclose()

        self.assertEqual(store.bindings, {"user-1": backend.id})
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(backend.close_calls, 1)

    async def test_cancelled_recovery_health_check_closes_candidate(self) -> None:
        restored = _FakeBackend("persisted-id")
        restored.block_command = "echo ok"
        client = _FakeClient()
        client.connected[restored.id] = restored
        manager = await self._manager(
            client,
            _FakeState({"user-1": restored.id}),
        )

        get_task = asyncio.create_task(manager.get(_key("user-1")))
        entered = await asyncio.to_thread(restored.execute_entered.wait, 1)
        self.assertTrue(entered)
        get_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await get_task
        restored.execute_gate.set()

        await manager.aclose()

        self.assertEqual(restored.close_calls, 1)
        self.assertEqual(client.destroy_calls, [])


class OpenSandboxDetailsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.managers: list[OpenSandboxManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.aclose()

    async def _manager(
        self,
        client: _FakeClient,
        store: _FakeState,
    ) -> OpenSandboxManager:
        manager = _new_manager(
            client=client,
            state=store,
            warm_pool_size=0,
        )
        self.managers.append(manager)
        await manager.start()
        return manager

    async def test_no_binding_returns_none_without_sandbox_side_effects(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        manager = await self._manager(client, store)

        details = await manager.get_details(_key("user-1"))

        self.assertIsNone(details)
        self.assertEqual(store.get_calls, ["user-1"])
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.connect_calls, [])
        self.assertEqual(client.inspect_calls, [])
        self.assertEqual(client.destroy_calls, [])
        self.assertEqual(store.save_calls, [])

    async def test_memory_details_use_backend_without_renewing_or_inspecting(
        self,
    ) -> None:
        info = _runtime_info("sandbox-1")
        client = _FakeClient(
            lambda sandbox_id: _FakeBackend(sandbox_id, runtime_info=info)
        )
        store = _FakeState()
        manager = await self._manager(client, store)
        await manager.get(_key("user-1"))
        backend = client.backends[0]
        renew_count = len(backend.renew_calls)
        create_count = client.create_calls
        save_count = len(store.save_calls)

        details = await manager.get_details(_key("user-1"))

        self.assertIsInstance(details, OpenSandboxDetails)
        assert details is not None
        self.assertEqual(details.owner_key, "user-1")
        self.assertEqual(details.sandbox_id, info.sandbox_id)
        self.assertTrue(details.cached)
        self.assertEqual(details.available, info.available)
        self.assertEqual(details.healthy, info.healthy)
        self.assertEqual(details.status, info.status)
        self.assertEqual(details.created_at, info.created_at)
        self.assertEqual(details.expires_at, info.expires_at)
        self.assertEqual(details.image, info.image)
        self.assertEqual(details.platform, info.platform)
        self.assertEqual(details.metadata, info.metadata)
        self.assertIsNone(details.unavailable_reason)
        self.assertEqual(backend.runtime_info_calls, 1)
        self.assertEqual(len(backend.renew_calls), renew_count)
        self.assertEqual(client.create_calls, create_count)
        self.assertEqual(client.inspect_calls, [])
        self.assertEqual(len(store.save_calls), save_count)

    async def test_store_details_inspect_without_connecting_caching_or_mutating(
        self,
    ) -> None:
        info = _runtime_info("persisted-id")
        client = _FakeClient()
        client.inspection_results[info.sandbox_id] = info
        store = _FakeState({"user-1": info.sandbox_id})
        manager = await self._manager(client, store)

        first = await manager.get_details(_key("user-1"))
        second = await manager.get_details(_key("user-1"))

        self.assertIsInstance(first, OpenSandboxDetails)
        assert first is not None
        self.assertEqual(first.owner_key, "user-1")
        self.assertEqual(first.sandbox_id, info.sandbox_id)
        self.assertFalse(first.cached)
        self.assertEqual(second, first)
        self.assertEqual(client.inspect_calls, [info.sandbox_id, info.sandbox_id])
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.connect_calls, [])
        self.assertEqual(store.save_calls, [])
        self.assertEqual(store.bindings, {"user-1": info.sandbox_id})

    async def test_unavailable_store_details_remain_read_only(self) -> None:
        for reason in ("not_found", "unreachable"):
            with self.subTest(reason=reason):
                info = _runtime_info(
                    "persisted-id",
                    available=False,
                    healthy=False,
                    unavailable_reason=reason,
                )
                client = _FakeClient()
                client.inspection_results[info.sandbox_id] = info
                store = _FakeState({"user-1": info.sandbox_id})
                manager = await self._manager(client, store)

                first = await manager.get_details(_key("user-1"))
                second = await manager.get_details(_key("user-1"))

                assert first is not None
                self.assertFalse(first.available)
                self.assertFalse(first.healthy)
                self.assertEqual(first.unavailable_reason, reason)
                self.assertEqual(second, first)
                self.assertEqual(
                    client.inspect_calls, [info.sandbox_id, info.sandbox_id]
                )
                self.assertEqual(client.create_calls, 0)
                self.assertEqual(client.connect_calls, [])
                self.assertEqual(store.save_calls, [])

    async def test_store_failure_is_not_reported_as_missing_binding(self) -> None:
        client = _FakeClient()
        store = _FakeState()
        store.get_error = RuntimeError("database unavailable")
        manager = await self._manager(client, store)

        with self.assertRaises(OpenSandboxStateError) as context:
            await manager.get_details(_key("user-1"))

        self.assertIsInstance(context.exception.__cause__, RuntimeError)
        self.assertEqual(client.inspect_calls, [])
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.connect_calls, [])


class _ReconnectableFakeClient(_FakeClient):
    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        backend = await super().create(metadata=metadata)
        self.connected[backend.id] = backend
        return backend


class _FailingCreateClient(_FakeClient):
    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        self.create_calls += 1
        self.create_metadata.append(dict(metadata or {}))
        raise RuntimeError("warmup failed")


class _BlockingCloseFailingCreateClient(_FailingCreateClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.release_close.wait()
        await super().aclose()


class _ResettableLocalBackend(LocalShellBackend):
    """在临时目录执行真实清理命令，同时模拟远端生命周期扩展"""

    enable_capture_offload = False

    def __init__(self, sandbox_id: str, workspace: Path) -> None:
        super().__init__(root_dir=workspace, virtual_mode=False, inherit_env=True)
        self._test_id = sandbox_id
        self.renew_calls: list[timedelta] = []
        self.close_calls = 0

    @property
    def id(self) -> str:
        return self._test_id

    def renew(self, timeout: timedelta) -> None:
        self.renew_calls.append(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        self.renew(timeout)

    def close(self) -> None:
        self.close_calls += 1

    async def aclose(self) -> None:
        self.close()

    @contextmanager
    def _rooted_file_operation(self) -> Iterator[None]:
        yield


def _install_reset_barrier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    opened: Path,
    release: Path,
) -> None:
    original_script = _rooted_protocol._ROOTED_HELPER_SCRIPT
    function_start = original_script.index("def reset_workspace(")
    function_end = original_script.find("\ndef ", function_start + 1)
    function_source = original_script[function_start:function_end]
    statement = "        opened = os.fstat(root_descriptor)"
    barrier = (
        statement
        + "\n"
        + f"        open({str(opened)!r}, 'x').close()\n"
        + f"        while not os.path.exists({str(release)!r}):\n"
        + "            time.sleep(0.001)"
    )
    assert function_source.count(statement) == 1
    monkeypatch.setattr(
        _rooted_protocol,
        "_ROOTED_HELPER_SCRIPT",
        (
            original_script[:function_start]
            + function_source.replace(statement, barrier)
            + original_script[function_end:]
        ),
    )


@pytest.mark.asyncio
async def test_manager_reconnect_and_destroy_aliases() -> None:
    client = _FakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        first = await manager.get(_key("user-1"))

        assert await manager.reconnect(_key("user-1")) is first

        await manager.destroy(_key("user-1"))
        assert client.destroy_calls == [first.id]
        assert store.bindings == {}
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manager_reconnect_creates_when_no_binding_exists() -> None:
    client = _FakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        backend = await manager.reconnect(_key("user-1"))

        assert backend.id == "sandbox-1"
        assert client.create_calls == 1
        assert store.bindings == {"user-1": "sandbox-1"}
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manager_lifecycle_uses_backend_coroutines_without_thread_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    thread_bridge_calls = 0

    async def reject_thread_bridge(*_args: object, **_kwargs: object) -> None:
        nonlocal thread_bridge_calls
        thread_bridge_calls += 1
        raise AssertionError("manager lifecycle must not use asyncio.to_thread")

    try:
        with monkeypatch.context() as context:
            context.setattr(asyncio, "to_thread", reject_thread_bridge)
            await manager.get(_key("user-1"))
        assert thread_bridge_calls == 0
    finally:
        await manager.aclose()


def test_manager_provides_rooted_middleware_for_final_backend() -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    manager = _new_manager(client=client, warm_pool_size=0)
    backend = cast(BackendProtocol, object())
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/**"],
            mode="deny",
        )
    ]

    middleware = manager.build_agent_middleware(
        backend,
        permissions=permissions,
    )

    assert len(middleware) == 1
    assert isinstance(middleware[0], AgentMiddleware)
    assert middleware[0].name == "FilesystemMiddleware"


def test_manager_omits_rooted_middleware_when_workspace_root_is_disabled() -> None:
    """Use raw Sandbox paths without rooted middleware when no root is configured."""

    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": None})
    manager = _new_manager(client=client, warm_pool_size=0)
    backend = cast(BackendProtocol, object())
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/**"],
            mode="deny",
        )
    ]

    middleware = manager.build_agent_middleware(
        backend,
        permissions=permissions,
    )

    assert middleware == ()


@pytest.mark.asyncio
async def test_manager_forwards_route_permissions_to_rooted_middleware(
    tmp_path: Path,
) -> None:
    """Enforce route permissions through the manager-produced middleware."""

    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": "/workspace"})
    manager = _new_manager(client=client, warm_pool_size=0)
    store = InMemoryStore()
    backend = CompositeBackend(
        default=LocalShellBackend(
            root_dir=tmp_path,
            virtual_mode=False,
            inherit_env=True,
        ),
        routes={
            "/policies/": StoreBackend(
                namespace=lambda _runtime: ("tests", "manager-policies")
            )
        },
    )
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/private/**"],
            mode="deny",
        )
    ]
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/private/rule.md",
                            "content": "blocked\n",
                        },
                        "id": "call-manager-private-policy",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="write attempted"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=manager.build_agent_middleware(
            backend,
            permissions=permissions,
        ),
        permissions=permissions,
        store=store,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "写入私有策略"}]}
    )

    write_result = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "write_file"
    )
    assert write_result.status == "error"
    assert "permission denied" in str(write_result.content).lower()
    assert (
        await store.aget(
            ("tests", "manager-policies"),
            "/private/rule.md",
        )
        is None
    )


@pytest.mark.asyncio
async def test_reset_clears_only_workspace_and_keeps_stable_backend(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".hidden").write_text("hidden", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "data.txt").write_text("data", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "keep.txt"
    outside_file.write_text("keep", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)

    client = _FakeClient(
        lambda sandbox_id: _ResettableLocalBackend(sandbox_id, workspace)
    )
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        backend = await manager.get(_key("user-1"))
        assert isinstance(backend, RootedOpenSandboxBackend)
        sandbox_id = backend.id

        await manager.reset(_key("user-1"))

        assert list(workspace.iterdir()) == []
        assert outside_file.read_text(encoding="utf-8") == "keep"
        assert await manager.reconnect(_key("user-1")) is backend
        assert backend.id == sandbox_id
        assert client.create_calls == 1
        assert client.destroy_calls == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_rejects_workspace_root_replaced_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    opened = tmp_path / "reset-opened"
    release = tmp_path / "reset-release"
    _install_reset_barrier(
        monkeypatch,
        opened=opened,
        release=release,
    )
    client = _FakeClient(
        lambda sandbox_id: _ResettableLocalBackend(sandbox_id, workspace)
    )
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("user-1"))
        reset_task = asyncio.create_task(manager.reset(_key("user-1")))
        deadline = asyncio.get_running_loop().time() + 1
        while not opened.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        if not opened.exists():
            await reset_task
        assert opened.exists(), "manager reset did not use the rooted helper"
        workspace.rename(tmp_path / "detached-workspace")
        workspace.symlink_to(outside, target_is_directory=True)
        release.touch()

        with pytest.raises(OpenSandboxResetError):
            await reset_task

        assert sentinel.read_text(encoding="utf-8") == "outside sentinel"
        assert list((tmp_path / "detached-workspace").iterdir()) == []
    finally:
        release.touch(exist_ok=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_cancellation_waits_for_helper_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    opened = tmp_path / "cancel-reset-opened"
    release = tmp_path / "cancel-reset-release"
    _install_reset_barrier(
        monkeypatch,
        opened=opened,
        release=release,
    )
    client = _FakeClient(
        lambda sandbox_id: _ResettableLocalBackend(sandbox_id, workspace)
    )
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("user-1"))
        reset_task = asyncio.create_task(manager.reset(_key("user-1")))
        deadline = asyncio.get_running_loop().time() + 1
        while not opened.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        assert opened.exists(), "reset helper did not reach the cancellation barrier"

        reset_task.cancel()
        await asyncio.sleep(0.05)
        assert not reset_task.done()
        release.touch()
        with pytest.raises(asyncio.CancelledError):
            await reset_task

        assert list(workspace.iterdir()) == []
    finally:
        release.touch(exist_ok=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_response_loss_is_reported_without_replay(tmp_path: Path) -> None:
    class ResponseLossBackend(_ResettableLocalBackend):
        def __init__(self, sandbox_id: str, workspace: Path) -> None:
            super().__init__(sandbox_id, workspace)
            self.reset_calls = 0

        async def aexecute(
            self,
            command: str,
            *,
            timeout: int | None = None,
        ) -> ExecuteResponse:
            self.reset_calls += 1
            await super().aexecute(command, timeout=timeout)
            raise ConnectionError("reset response lost")

    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    created: list[ResponseLossBackend] = []

    def backend_factory(sandbox_id: str) -> ResponseLossBackend:
        backend = ResponseLossBackend(sandbox_id, workspace)
        created.append(backend)
        return backend

    client = _FakeClient(backend_factory)
    client.config = client.config.model_copy(update={"workspace_root": str(workspace)})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("user-1"))

        with pytest.raises(OpenSandboxResetError):
            await manager.reset(_key("user-1"))

        assert created[0].reset_calls == 1
        assert list(workspace.iterdir()) == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reset_requires_a_managed_key() -> None:
    client = _FakeClient()
    client.config = client.config.model_copy(update={"workspace_root": None})
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        with pytest.raises(OpenSandboxResetError, match="workspace_root"):
            await manager.reset(_key("user-1"))
        assert client.create_calls == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_strict_startup_warmup_propagates_creation_failure() -> None:
    """严格预热失败必须阻止管理器进入可用状态"""

    manager = _new_manager(
        client=_FailingCreateClient(),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )

    with pytest.raises(RuntimeError, match="warmup failed"):
        await manager.start()
    await manager.aclose()


@pytest.mark.asyncio
async def test_repeated_cancellation_settles_late_custom_warm_backend() -> None:
    client = _RepeatedCancellationWarmClient()
    manager = _new_manager(
        client=client,
        state=_WarmLeaseState(renews=True),
        warm_pool_size=1,
    )
    startup = asyncio.create_task(manager.start())
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(client.create_entered.wait(), timeout=1)
        startup_owner = manager._start_task
        assert startup_owner is not None
        startup_owner.cancel("warm startup cancelled")
        await asyncio.wait_for(client.first_cancellation.wait(), timeout=1)
        startup_owner.cancel("warm startup cancelled again")
        await asyncio.sleep(0)
        settled_before_late_result = startup_owner.done()
        child_received_repeated_cancellation = client.repeated_cancellation.is_set()
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        close_settled_before_late_result = closing.done()

        client.release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
        await asyncio.wait_for(client.create_returned.wait(), timeout=1)
        await closing

        backend = cast(_FakeBackend, client.backends[0])
        assert not settled_before_late_result
        assert not close_settled_before_late_result
        assert not child_received_repeated_cancellation
        assert client.destroy_calls == [backend.id]
        assert backend.close_calls == 1
    finally:
        client.release_create.set()
        await asyncio.gather(startup, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_warm_renewal_failure_settles_late_custom_backend() -> None:
    client = _RepeatedCancellationWarmClient()
    manager = _new_manager(
        client=client,
        state=_WarmLeaseState(renews=False),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    startup = asyncio.create_task(manager.start())
    try:
        await asyncio.wait_for(client.create_entered.wait(), timeout=1)
        await asyncio.wait_for(client.first_cancellation.wait(), timeout=1)
        assert not startup.done()

        client.release_create.set()
        with pytest.raises(
            OpenSandboxStateOwnershipError,
            match="Warm slot 0 was lost",
        ):
            await startup
        await asyncio.wait_for(client.create_returned.wait(), timeout=1)
        await manager.aclose()

        backend = cast(_FakeBackend, client.backends[0])
        assert client.destroy_calls == [backend.id]
        assert backend.close_calls == 1
    finally:
        client.release_create.set()
        await asyncio.gather(startup, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_native_client_retains_cancelled_create_reclaim_ownership() -> None:
    backend = _FakeBackend("sandbox-native")
    client = _NativeOwnershipClient(backend)
    manager = _new_manager(
        client=client,
        state=_WarmLeaseState(renews=True),
        warm_pool_size=1,
    )
    startup = asyncio.create_task(manager.start())
    try:
        await asyncio.wait_for(client.create_entered.wait(), timeout=1)
        startup_owner = manager._start_task
        assert startup_owner is not None
        startup_owner.cancel("warm startup cancelled")
        await asyncio.sleep(0)
        startup_owner.cancel("warm startup cancelled again")
        client.release_create.set()

        with pytest.raises(asyncio.CancelledError):
            await startup
        await manager.aclose()

        assert client.destroy_calls == []
        assert backend.kill_calls == 1
        assert backend.close_calls == 1
    finally:
        client.release_create.set()
        await asyncio.gather(startup, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.parametrize(
    ("value", "error_type"),
    (
        pytest.param(True, TypeError, id="boolean"),
        pytest.param("1", TypeError, id="string"),
        pytest.param(-0.01, ValueError, id="negative"),
        pytest.param(float("inf"), ValueError, id="infinity"),
        pytest.param(float("nan"), ValueError, id="nan"),
    ),
)
def test_manager_rejects_invalid_settlement_timeout(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        _new_manager(
            client=_FakeClient(),
            warm_pool_size=0,
            settlement_timeout=value,
        )


@pytest.mark.parametrize("value", (None, 0, 0.01))
def test_manager_accepts_non_negative_settlement_timeout(
    value: float | None,
) -> None:
    _new_manager(
        client=_FakeClient(),
        warm_pool_size=0,
        settlement_timeout=value,
    )


@pytest.mark.parametrize("settlement_timeout", (0, 0.01))
async def test_manager_close_timeout_retains_shared_cleanup_for_second_close(
    settlement_timeout: float,
) -> None:
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
        settlement_timeout=settlement_timeout,
    )
    await manager.start()
    backend = cast(_FakeBackend, client.backends[0])
    backend.close_gate = threading.Event()
    timeout_type = getattr(
        tinkerfin_sandbox,
        "OpenSandboxSettlementTimeoutError",
        None,
    )
    assert timeout_type is not None

    try:
        with pytest.raises(timeout_type) as captured:
            await manager.aclose()

        assert captured.value.timeout == settlement_timeout
        close_entered = await asyncio.to_thread(backend.close_entered.wait, 1)
        assert close_entered
        assert backend.close_calls == 1
        assert client.destroy_calls == [backend.id]
        with pytest.raises(OpenSandboxManagerClosedError):
            await manager.get("owner-1")

        backend.close_gate.set()
        if settlement_timeout == 0:
            await _eventually(lambda: client.close_calls == 1)
        await manager.aclose()

        assert backend.close_calls == 1
        assert client.destroy_calls == [backend.id]
        assert client.close_calls == 1
    finally:
        if backend.close_gate is not None:
            backend.close_gate.set()
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.parametrize(
    ("body_error", "error_type"),
    (
        pytest.param(ValueError("body failed"), ValueError, id="business-error"),
        pytest.param(
            asyncio.CancelledError("body cancelled"),
            asyncio.CancelledError,
            id="body-cancellation",
        ),
    ),
)
async def test_manager_context_body_failure_outranks_close_timeout(
    body_error: BaseException,
    error_type: type[BaseException],
) -> None:
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
        settlement_timeout=0.01,
    )
    backend: _FakeBackend | None = None
    try:
        with pytest.raises(error_type) as captured:
            async with manager:
                backend = cast(_FakeBackend, client.backends[0])
                backend.close_gate = threading.Event()
                raise body_error

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        assert captured.value is body_error
        assert "OpenSandboxSettlementTimeoutError" in notes
    finally:
        if backend is not None and backend.close_gate is not None:
            backend.close_gate.set()
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_context_caller_cancellation_retains_body_failure() -> None:
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
    )
    body_error = ValueError("body failed")

    async def use_manager() -> None:
        async with manager:
            backend = cast(_FakeBackend, client.backends[0])
            backend.close_gate = threading.Event()
            raise body_error

    operation = asyncio.create_task(use_manager())
    backend: _FakeBackend | None = None
    try:
        await asyncio.wait_for(client.create_entered.wait(), timeout=1)
        backend = cast(_FakeBackend, client.backends[0])
        entered = await asyncio.to_thread(backend.close_entered.wait, 1)
        assert entered

        operation.cancel("context cleanup cancelled")
        with pytest.raises(asyncio.CancelledError) as captured:
            await operation

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        assert "OpenSandbox context body also failed: ValueError: body failed" in notes

        assert backend.close_gate is not None
        backend.close_gate.set()
        await manager.aclose()
        assert backend.close_calls == 1
        assert client.destroy_calls == [backend.id]
    finally:
        if backend is not None and backend.close_gate is not None:
            backend.close_gate.set()
        await asyncio.gather(operation, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_start_failure_outranks_close_timeout() -> None:
    client = _BlockingCloseFailingCreateClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
        settlement_timeout=0.01,
    )
    try:
        with pytest.raises(RuntimeError, match="warmup failed") as captured:
            await manager.__aenter__()

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        assert "OpenSandboxSettlementTimeoutError" in notes
        assert client.close_entered.is_set()
    finally:
        client.release_close.set()
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_start_cleanup_caller_cancellation_retains_start_error() -> None:
    client = _BlockingCloseFailingCreateClient()
    manager = _new_manager(
        client=client,
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    opening = asyncio.create_task(manager.__aenter__())
    try:
        await asyncio.wait_for(client.close_entered.wait(), timeout=1)
        opening.cancel("startup cleanup cancelled")

        with pytest.raises(asyncio.CancelledError) as captured:
            await opening

        notes = "\n".join(getattr(captured.value, "__notes__", ()))
        assert "OpenSandbox startup also failed: RuntimeError: warmup failed" in notes

        client.release_close.set()
        await manager.aclose()
        assert client.close_calls == 1
    finally:
        client.release_close.set()
        await asyncio.gather(opening, return_exceptions=True)
        await asyncio.gather(manager.aclose(), return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_owner_claim_release() -> None:
    client = _FakeClient()
    client.create_gate = asyncio.Event()
    state = _BlockingOwnerReleaseState()
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    await manager.start()
    operation = asyncio.create_task(manager.get("owner-1"))
    successor: OpenSandboxOwnerClaim | None = None
    try:
        await client.create_entered.wait()
        operation.cancel("owner operation cancelled")
        await state.release_started.wait()
        operation.cancel("owner operation cancelled again")
        state.release_gate.set()

        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not state.release_cancelled.is_set()
        successor = await asyncio.wait_for(
            state.acquire_owner("owner-1"),
            timeout=0.2,
        )
    finally:
        state.release_gate.set()
        await asyncio.gather(operation, return_exceptions=True)
        if successor is not None:
            await InMemoryOpenSandboxState.release_owner(state, successor)
        if state.claim is not None:
            await InMemoryOpenSandboxState.release_owner(state, state.claim)
        await manager.aclose()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_warm_claim_release() -> None:
    client = _ObservedWarmCreateClient()
    client.create_gate = asyncio.Event()
    state = _BlockingWarmReleaseState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)
    startup = asyncio.create_task(manager.start())
    successor: OpenSandboxWarmClaim | None = None
    try:
        await client.create_entered.wait()
        create_task = client.current_create_task
        assert create_task is not None
        create_task.cancel("warm creation cancelled")
        await state.release_started.wait()
        create_task.cancel("warm creation cancelled again")
        state.release_gate.set()

        with pytest.raises(asyncio.CancelledError):
            await startup
        assert not state.release_cancelled.is_set()
        successor = await state.claim_warm_slot()
        assert successor is not None
    finally:
        state.release_gate.set()
        await asyncio.gather(startup, return_exceptions=True)
        if successor is not None:
            await InMemoryOpenSandboxState.release_warm(state, successor)
        if state.claim is not None:
            await InMemoryOpenSandboxState.release_warm(state, state.claim)
        close_result = (await asyncio.gather(manager.aclose(), return_exceptions=True))[
            0
        ]
        if isinstance(close_result, BaseException):
            await state.aclose()
            await client.aclose()


@pytest.mark.asyncio
async def test_close_after_cancelled_startup_still_closes_state_and_client() -> None:
    client = _CancelledWarmupClient()
    state = _CloseObservedState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)

    with pytest.raises(asyncio.CancelledError):
        await manager.start()

    close_result = (await asyncio.gather(manager.aclose(), return_exceptions=True))[0]
    state_close_calls = state.close_calls
    client_close_calls = client.close_calls
    if isinstance(close_result, BaseException):
        await state.aclose()
        await client.aclose()

    assert close_result is None
    assert state_close_calls == 1
    assert client_close_calls == 1


@pytest.mark.asyncio
async def test_shared_state_prevents_cross_manager_duplicate_create() -> None:
    """两个进程级 manager 竞争同一 owner 时只能有一个创建者"""

    client = _ReconnectableFakeClient()
    store = _FakeState()
    first_manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    second_manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await first_manager.start()
    await second_manager.start()
    try:
        first, second = await asyncio.gather(
            first_manager.get(_key("user-1")),
            second_manager.get(_key("user-1")),
        )
    finally:
        await first_manager.aclose()
        await second_manager.aclose()

    assert first.id == second.id == "sandbox-1"
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_manager_tags_on_demand_create_with_hashed_owner() -> None:
    client = _ReconnectableFakeClient()
    manager = _new_manager(client=client, warm_pool_size=0)
    await manager.start()
    try:
        await manager.get(_key("e2e-c1e84f194a-shared/agent-b"))
    finally:
        await manager.aclose()

    owner_label = client.create_metadata[0]["tinkerfin.ai/owner"]
    assert len(owner_label) <= 63
    assert owner_label[0].isalnum()
    assert owner_label[-1].isalnum()
    assert all(character.isalnum() or character in "-_." for character in owner_label)
    assert "e2e-c1e84f194a-shared" not in owner_label
    assert "agent-b" not in owner_label


async def test_sql_states_share_one_global_warm_pool(tmp_path: Path) -> None:
    client = _ReconnectableFakeClient()
    url = f"sqlite+aiosqlite:///{tmp_path / 'manager-state.db'}"
    first_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(url=url, namespace="test"),
        warm_pool_size=1,
    )
    second_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(url=url, namespace="test"),
        warm_pool_size=1,
    )
    await asyncio.gather(first_manager.start(), second_manager.start())
    try:
        assert client.create_calls == 1

        handle = await second_manager.get(_key("user-1"))
        await _eventually(lambda: client.create_calls == 2)

        assert handle.id == "sandbox-1"
        assert client.create_calls == 2
    finally:
        await asyncio.gather(first_manager.aclose(), second_manager.aclose())


@pytest.mark.asyncio
async def test_manager_renews_sql_owner_claim_during_slow_create(
    tmp_path: Path,
) -> None:
    client = _ReconnectableFakeClient()
    client.create_gate = asyncio.Event()
    url = f"sqlite+aiosqlite:///{tmp_path / 'owner-renewal.db'}"
    first_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(
            url=url,
            namespace="test",
            lease_ttl=0.3,
            poll_interval=0.01,
        ),
        warm_pool_size=0,
    )
    second_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(
            url=url,
            namespace="test",
            lease_ttl=0.3,
            poll_interval=0.01,
        ),
        warm_pool_size=0,
    )
    await asyncio.gather(first_manager.start(), second_manager.start())
    first_get = asyncio.create_task(first_manager.get(_key("user-1")))
    await client.create_entered.wait()
    second_get = asyncio.create_task(second_manager.get(_key("user-1")))
    try:
        await asyncio.sleep(0.5)
        assert client.create_calls == 1
    finally:
        client.create_gate.set()
        results = await asyncio.gather(first_get, second_get, return_exceptions=True)
        await asyncio.gather(first_manager.aclose(), second_manager.aclose())

    handles: list[OpenSandboxHandle | RootedOpenSandboxBackend] = []
    for result in results:
        assert isinstance(result, (OpenSandboxHandle, RootedOpenSandboxBackend))
        handles.append(result)
    assert {handle.id for handle in handles} == {"sandbox-1"}


@pytest.mark.asyncio
async def test_manager_renews_sql_warm_claim_during_slow_create(
    tmp_path: Path,
) -> None:
    client = _ReconnectableFakeClient()
    client.create_gate = asyncio.Event()
    url = f"sqlite+aiosqlite:///{tmp_path / 'warm-renewal.db'}"
    first_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(
            url=url,
            namespace="test",
            lease_ttl=0.3,
            poll_interval=0.01,
        ),
        warm_pool_size=1,
    )
    second_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(
            url=url,
            namespace="test",
            lease_ttl=0.3,
            poll_interval=0.01,
        ),
        warm_pool_size=1,
    )
    first_start = asyncio.create_task(first_manager.start())
    await client.create_entered.wait()
    await asyncio.sleep(0.5)
    second_start = asyncio.create_task(second_manager.start())
    try:
        await asyncio.sleep(0.05)
        assert client.create_calls == 1
    finally:
        client.create_gate.set()
        await asyncio.gather(first_start, second_start, return_exceptions=True)
        await asyncio.gather(first_manager.aclose(), second_manager.aclose())


@pytest.mark.asyncio
async def test_manager_drains_durable_cleanup_queue_on_start(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cleanup-start.db'}"
    seeded_state = SQLAlchemyOpenSandboxState(url=url, namespace="test")
    await seeded_state.start(warm_pool_size=0)
    await seeded_state.enqueue_cleanup("orphan-sandbox")
    await seeded_state.aclose()

    client = _FakeClient()
    manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(url=url, namespace="test"),
        warm_pool_size=0,
    )
    await manager.start()
    await manager.aclose()

    assert client.destroy_calls == ["orphan-sandbox"]
    checking_state = SQLAlchemyOpenSandboxState(url=url, namespace="test")
    await checking_state.start(warm_pool_size=0)
    try:
        assert await checking_state.claim_cleanup() is None
    finally:
        await checking_state.aclose()


@pytest.mark.asyncio
async def test_manager_persists_failed_replacement_cleanup(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cleanup-replacement.db'}"
    client = _ReconnectableFakeClient()
    manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(url=url, namespace="test"),
        warm_pool_size=0,
    )
    await manager.start()
    await manager.get(_key("user-1"))
    old_backend = client.backends[0]
    old_backend.healthy = False
    client.destroy_errors[old_backend.id] = RuntimeError("destroy unavailable")

    replacement = await manager.get(_key("user-1"))
    await manager.aclose()

    assert replacement.id == "sandbox-2"
    checking_state = SQLAlchemyOpenSandboxState(url=url, namespace="test")
    await checking_state.start(warm_pool_size=0)
    try:
        cleanup = await checking_state.claim_cleanup()
        assert cleanup is not None
        assert cleanup.sandbox_id == "sandbox-1"
        await checking_state.release_cleanup(cleanup)
    finally:
        await checking_state.aclose()


@pytest.mark.asyncio
async def test_manager_retries_persisted_cleanup_in_background(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cleanup-background.db'}"
    client = _ReconnectableFakeClient()
    state = _CleanupObservedSQLState(url=url, namespace="test")
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=0,
    )
    await manager.start()
    handle = await manager.get(_key("user-1"))
    old_backend = client.backends[0]
    old_backend.healthy = False
    client.destroy_errors[old_backend.id] = RuntimeError("temporary failure")

    await manager.get(_key("user-1"))
    client.destroy_errors.clear()
    try:
        await _eventually(
            lambda: client.destroy_calls.count(old_backend.id) >= 2,
        )
        await asyncio.wait_for(state.cleanup_completed.wait(), timeout=1)
    finally:
        await manager.aclose()

    checking_state = SQLAlchemyOpenSandboxState(url=url, namespace="test")
    await checking_state.start(warm_pool_size=0)
    try:
        assert await checking_state.claim_cleanup() is None
    finally:
        await checking_state.aclose()
    assert handle.id == "sandbox-2"


@pytest.mark.asyncio
async def test_manager_renews_sql_cleanup_claim_during_slow_destroy(
    tmp_path: Path,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cleanup-renewal.db'}"
    seeded_state = SQLAlchemyOpenSandboxState(url=url, namespace="test")
    await seeded_state.start(warm_pool_size=0)
    await seeded_state.enqueue_cleanup("orphan-sandbox")
    await seeded_state.aclose()

    client = _FakeClient()
    client.destroy_gate = asyncio.Event()
    first_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(
            url=url,
            namespace="test",
            lease_ttl=0.3,
            poll_interval=0.01,
        ),
        warm_pool_size=0,
    )
    second_manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(
            url=url,
            namespace="test",
            lease_ttl=0.3,
            poll_interval=0.01,
        ),
        warm_pool_size=0,
    )
    first_start = asyncio.create_task(first_manager.start())
    await client.destroy_entered.wait()
    await asyncio.sleep(0.5)
    second_start = asyncio.create_task(second_manager.start())
    try:
        await asyncio.sleep(0.05)
        assert client.destroy_calls == ["orphan-sandbox"]
    finally:
        client.destroy_gate.set()
        results = await asyncio.gather(
            first_start,
            second_start,
            return_exceptions=True,
        )
        await asyncio.gather(first_manager.aclose(), second_manager.aclose())

    assert not any(isinstance(result, BaseException) for result in results)


@pytest.mark.asyncio
async def test_state_get_adopts_authoritative_binding_change() -> None:
    """Manager 必须采用 State 权威绑定，而不是返回健康但陈旧的本地缓存"""

    client = _ReconnectableFakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        authoritative = _FakeBackend("sandbox-external")
        client.connected[authoritative.id] = authoritative
        store.bindings["user-1"] = authoritative.id

        refreshed = await manager.get(_key("user-1"))

        assert refreshed is handle
        assert refreshed.id == authoritative.id
        assert old_backend.close_calls == 1
        assert client.destroy_calls == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_state_recreate_cleans_cached_and_authoritative_old_ids() -> None:
    """跨进程改绑后 recreate 必须回收本地旧实例和持久化权威旧实例"""

    client = _ReconnectableFakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    try:
        handle = await manager.get(_key("user-1"))
        store.bindings["user-1"] = "sandbox-external"

        recreated = await manager.recreate(_key("user-1"))

        assert recreated is handle
        assert recreated.id == "sandbox-2"
        assert len(client.destroy_calls) == 2
        assert set(client.destroy_calls) == {"sandbox-1", "sandbox-external"}
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancelled_recreate_tracks_distinct_authoritative_old_id() -> None:
    client = _ReconnectableFakeClient()
    store = _FakeState()
    manager = _new_manager(
        client=client,
        state=store,
        warm_pool_size=0,
    )
    await manager.start()
    execute_task: asyncio.Task[ExecuteResponse] | None = None
    recreate_task: asyncio.Task[OpenSandboxHandle | RootedOpenSandboxBackend] | None = (
        None
    )
    try:
        handle = await manager.get(_key("user-1"))
        old_backend = client.backends[0]
        old_backend.block_command = "long-running"
        execute_task = asyncio.create_task(
            asyncio.to_thread(handle.execute, "long-running")
        )
        assert await asyncio.to_thread(old_backend.execute_entered.wait, 1)
        store.bindings["user-1"] = "sandbox-external"

        recreate_task = asyncio.create_task(manager.recreate(_key("user-1")))
        await _eventually(lambda: handle.id == "sandbox-2")
        recreate_task.cancel("caller stopped waiting for replacement cleanup")
        with pytest.raises(asyncio.CancelledError):
            await recreate_task

        old_backend.execute_gate.set()
        assert execute_task is not None
        await execute_task
    finally:
        for backend in client.backends:
            backend.execute_gate.set()
        if execute_task is not None:
            await asyncio.gather(execute_task, return_exceptions=True)
        if recreate_task is not None:
            await asyncio.gather(recreate_task, return_exceptions=True)
        await manager.aclose()

    assert store.bindings == {"user-1": "sandbox-2"}
    assert len(client.destroy_calls) == 2
    assert set(client.destroy_calls) == {"sandbox-1", "sandbox-external"}
    assert old_backend.close_calls == 1


async def test_manager_uses_state_as_its_allocation_boundary() -> None:
    client = _FakeClient()
    state = InMemoryOpenSandboxState()
    manager = _new_manager(
        client=client,
        state=state,
        warm_pool_size=0,
    )
    try:
        await manager.start()
        handle = await manager.get(_key("user-1"))

        binding = await state.read_binding("user-1")
        assert binding is not None
        assert binding.sandbox_id == handle.id
    finally:
        await manager.aclose()


async def test_committed_warm_binding_is_not_destroyed_after_owner_loss() -> None:
    client = _FakeClient()
    state = _WarmBindingLossState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)
    await manager.start()
    warm_backend = client.backends[0]
    warm_backend.block_command = "echo ok"
    getting = asyncio.create_task(manager.get(_key("user-1")))
    try:
        assert await asyncio.to_thread(warm_backend.execute_entered.wait, 1)
        with pytest.raises(OpenSandboxStateOwnershipError):
            await asyncio.wait_for(getting, timeout=1)
    finally:
        warm_backend.execute_gate.set()
        await asyncio.gather(getting, return_exceptions=True)
        await manager.aclose()

    assert state.consume_calls == [("user-1", "sandbox-1")]
    assert state.bindings == {"user-1": "sandbox-1"}
    assert client.destroy_calls == []
    assert warm_backend.close_calls == 1


async def test_committed_on_demand_binding_survives_lost_commit_response() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.commit_before_save_error = True
    state.save_error = RuntimeError("database response was lost")
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    try:
        await manager.start()
        handle = await manager.get(_key("user-1"))

        assert handle.id == "sandbox-1"
        assert state.bindings == {"user-1": "sandbox-1"}
        assert state.save_calls == [("user-1", "sandbox-1")]
        assert len(state.get_calls) == 2
        assert client.destroy_calls == []
        assert client.backends[0].close_calls == 0
    finally:
        await manager.aclose()


async def test_cancelled_committed_bind_closes_locally_and_reconnects() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.cancel_after_save_commit = True
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    await manager.start()
    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="bind response was cancelled after commit",
        ):
            await manager.get(_key("user-1"))

        created = client.backends[0]
        state.cancel_after_save_commit = False
        reconnected = _FakeBackend("sandbox-1")
        client.connected["sandbox-1"] = reconnected
        handle = await manager.get(_key("user-1"))

        assert handle.id == "sandbox-1"
        assert client.create_calls == 1
        assert client.connect_calls == ["sandbox-1"]
        assert client.destroy_calls == []
        assert created.close_calls == 1
    finally:
        await manager.aclose()


async def test_unknown_bind_outcome_closes_without_destroying_candidate() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.save_error = RuntimeError("database write failed")
    state.read_error = RuntimeError("database read failed")
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    try:
        await manager.start()
        with pytest.raises(
            OpenSandboxStateError, match="fake state write failed"
        ) as caught:
            await manager.get(_key("user-1"))

        assert any("reconciliation" in note for note in caught.value.__notes__)
        assert len(state.get_calls) == 2
        assert client.destroy_calls == []
        assert client.backends[0].close_calls == 1
    finally:
        await manager.aclose()


async def test_replaced_binding_allows_only_the_candidate_to_be_destroyed() -> None:
    client = _FakeClient()
    state = _FakeState()
    state.save_error = RuntimeError("owner claim expired")
    state.read_override_enabled = True
    state.read_override = OpenSandboxBinding(
        sandbox_id="sandbox-authoritative",
        generation=99,
    )
    manager = _new_manager(client=client, state=state, warm_pool_size=0)
    try:
        await manager.start()
        with pytest.raises(OpenSandboxStateError, match="fake state write failed"):
            await manager.get(_key("user-1"))

        assert len(state.get_calls) == 2
        assert client.destroy_calls == ["sandbox-1"]
        assert "sandbox-authoritative" not in client.destroy_calls
        assert client.backends[0].close_calls == 1
    finally:
        await manager.aclose()


async def test_failed_replacement_preserves_the_committed_warm_sandbox() -> None:
    def backend_factory(sandbox_id: str) -> _FakeBackend:
        if sandbox_id == "sandbox-2":
            raise RuntimeError("replacement creation failed")
        return _FakeBackend(sandbox_id, healthy=False)

    client = _FakeClient(backend_factory)
    state = _FakeState()
    manager = _new_manager(client=client, state=state, warm_pool_size=1)
    try:
        await manager.start()
        warm_backend = client.backends[0]

        with pytest.raises(RuntimeError, match="replacement creation failed"):
            await manager.get(_key("user-1"))

        assert state.bindings == {"user-1": "sandbox-1"}
        assert client.destroy_calls == []
        assert warm_backend.close_calls == 1
    finally:
        await manager.aclose()
