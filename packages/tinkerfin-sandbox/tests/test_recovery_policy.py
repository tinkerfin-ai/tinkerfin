from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from opensandbox.config import ConnectionConfig
from test_backend import _FakeSandbox
from test_manager import _FakeBackend, _FakeClient, _FakeState, _new_manager

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxInitializationError,
    OpenSandboxRecoveryPolicy,
    OpenSandboxStateOwnershipError,
    UnexpectedOpenSandboxBackendError,
)


class _RecoveringClient(_FakeClient):
    def __init__(self, failures: list[Exception]) -> None:
        super().__init__()
        self.failures = failures
        self.connect_entered = asyncio.Event()
        self.connected["original"] = _FakeBackend("original")

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        self.connect_entered.set()
        if self.failures:
            self.connect_calls.append(sandbox_id)
            raise self.failures.pop(0)
        return await super().connect(sandbox_id)


def _fast_policy(*, recreate: bool = False) -> OpenSandboxRecoveryPolicy:
    return OpenSandboxRecoveryPolicy(
        initial_delay=0, max_delay=0, on_failure="recreate" if recreate else "raise"
    )


@pytest.mark.asyncio
async def test_transient_recovery_retries_only_the_original_id() -> None:
    client = _RecoveringClient(
        [
            OpenSandboxBackendTimeoutError("temporary timeout"),
            OpenSandboxBackendUnavailableError(
                "temporary network failure", context={"reason": "unreachable"}
            ),
        ]
    )
    state = _FakeState({"owner": "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy()
    ) as manager:
        backend = await manager.get("owner")
        assert backend.id == "original"
        assert client.connect_calls == ["original"] * 3
        assert state.bindings == {"owner": "original"}
        assert client.create_calls == 0
        assert client.destroy_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        OpenSandboxBackendUnavailableError(
            "auth", context={"reason": "authentication"}
        ),
        OpenSandboxBackendUnavailableError("denied", context={"reason": "permission"}),
        OpenSandboxBackendProtocolError("protocol"),
        OpenSandboxInitializationError(
            "initializer", cause=OpenSandboxBackendTimeoutError("initializer timeout")
        ),
        UnexpectedOpenSandboxBackendError("unknown 404 not found"),
    ],
)
async def test_nonrecoverable_failures_never_retry_or_recreate(
    failure: OpenSandboxBackendError,
) -> None:
    client = _RecoveringClient([failure])
    state = _FakeState({"owner": "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        with pytest.raises(type(failure)) as raised:
            await manager.get("owner")
        assert raised.value is failure
        assert client.connect_calls == ["original"]
        assert client.create_calls == 0
        assert client.destroy_calls == []
        assert state.bindings == {"owner": "original"}


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
async def test_confirmed_missing_skips_retries_and_obeys_the_failure_action(
    recreate: bool,
) -> None:
    client = _RecoveringClient(
        [OpenSandboxBackendUnavailableError("gone", context={"reason": "not_found"})]
    )
    state = _FakeState({"owner": "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=recreate)
    ) as manager:
        if recreate:
            backend = await manager.get("owner")
            assert backend.id != "original"
            assert state.bindings == {"owner": backend.id}
            assert client.destroy_calls == ["original"]
        else:
            with pytest.raises(OpenSandboxBackendUnavailableError) as raised:
                await manager.get("owner")
            assert raised.value.context["attempts"] == 1
            assert state.bindings == {"owner": "original"}
            assert client.destroy_calls == []
        assert client.connect_calls == ["original"]
        assert client.create_calls == int(recreate)


@pytest.mark.asyncio
async def test_reconnect_cannot_recreate_even_when_get_is_allowed_to() -> None:
    client = _RecoveringClient(
        [OpenSandboxBackendUnavailableError("gone", context={"reason": "not_found"})]
    )
    state = _FakeState({"owner": "original"})
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        with pytest.raises(OpenSandboxBackendUnavailableError):
            await manager.reconnect("owner")
        assert client.create_calls == 0
        assert client.destroy_calls == []
        assert state.bindings == {"owner": "original"}


@pytest.mark.asyncio
async def test_cancellation_during_retry_keeps_binding_and_instance() -> None:
    client = _RecoveringClient([OpenSandboxBackendTimeoutError("temporary")])
    state = _FakeState({"owner": "original"})
    async with _new_manager(client=client, state=state) as manager:
        getting = asyncio.create_task(manager.get("owner"))
        await client.connect_entered.wait()
        getting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await getting
        assert state.bindings == {"owner": "original"}
        assert client.create_calls == 0
        assert client.destroy_calls == []


@pytest.mark.asyncio
async def test_uncertain_connection_at_budget_expiry_cannot_authorize_recreation() -> (
    None
):
    class BlockedClient(_RecoveringClient):
        async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
            self.connect_calls.append(sandbox_id)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = BlockedClient([])
    state = _FakeState({"owner": "original"})
    policy = replace(_fast_policy(recreate=True), timeout=0.02)
    async with _new_manager(
        client=client, state=state, recovery_policy=policy
    ) as manager:
        with pytest.raises(OpenSandboxBackendUnavailableError) as raised:
            await asyncio.wait_for(manager.get("owner"), 1)
        assert raised.value.context["reason"] == "timeout"
        assert client.connect_calls == ["original"]
        assert client.create_calls == 0
        assert client.destroy_calls == []
        assert state.bindings == {"owner": "original"}


@pytest.mark.asyncio
async def test_lost_binding_does_not_authorize_disposal_of_the_local_handle() -> None:
    client = _FakeClient()
    state = _FakeState()
    async with _new_manager(
        client=client, state=state, recovery_policy=_fast_policy(recreate=True)
    ) as manager:
        handle = await manager.get("owner")
        state.bindings.clear()
        with pytest.raises(OpenSandboxStateOwnershipError):
            await manager.get("owner")
        assert handle.id == "sandbox-1"
        assert client.create_calls == 1
        assert client.destroy_calls == []


@pytest.mark.asyncio
async def test_recreate_rejects_a_local_handle_without_authoritative_binding() -> None:
    client = _FakeClient()
    state = _FakeState()
    async with _new_manager(client=client, state=state) as manager:
        handle = await manager.get("owner")
        state.bindings.clear()
        with pytest.raises(OpenSandboxStateOwnershipError):
            await manager.recreate("owner")
        assert handle.id == "sandbox-1"
        assert client.create_calls == 1
        assert client.destroy_calls == []
        assert client.backends[0].kill_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "initializer"])
async def test_manager_deadline_does_not_wait_for_native_connection_settlement(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    connection_cancelled = asyncio.Event()
    sandbox = _FakeSandbox("original")

    async def connect(*_args: object, **_kwargs: object) -> _FakeSandbox:
        if phase == "connect":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                connection_cancelled.set()
                raise
        return sandbox

    async def initialize(_backend: OpenSandboxBackend) -> None:
        if phase == "initializer":
            entered.set()
            await release.wait()

    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        AsyncMock(side_effect=connect),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(
            workspace_root=None, warm_pool_size=0, connect_timeout=timedelta(seconds=2)
        ),
        initializers=[initialize],
    )
    state = _FakeState({"owner": "original"})
    policy = replace(_fast_policy(recreate=True), timeout=0.02)
    async with _new_manager(
        client=client, state=state, recovery_policy=policy
    ) as manager:
        getting = asyncio.create_task(manager.get("owner"))
        try:
            await entered.wait()
            done, _ = await asyncio.wait({getting}, timeout=0.2)
            completed_in_budget = getting in done
        finally:
            release.set()
            with pytest.raises(OpenSandboxBackendUnavailableError) as raised:
                await getting
            assert raised.value.context["reason"] == "timeout"
        assert state.bindings == {"owner": "original"}
    assert completed_in_budget
    if phase == "initializer":
        assert sandbox.closed
    else:
        assert connection_cancelled.is_set()
    assert not sandbox.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_connection_settlement_retains_owner_claim_and_blocks_next_initializer(
    monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool
) -> None:
    entered, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first, second = _FakeSandbox("original"), _FakeSandbox("original")
    first.commands.result.exit_code = 0
    second.commands.result.exit_code = 0
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr("tinkerfin_sandbox.lifecycle.client.Sandbox.connect", connect)
    initialized = 0

    async def initialize(_backend: OpenSandboxBackend) -> None:
        nonlocal initialized
        initialized += 1
        if initialized != 1:
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settling.set()
            await release.wait()

    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(workspace_root=None, warm_pool_size=0),
        initializers=[initialize],
    )
    state = _FakeState({"owner": "original"})
    async with _new_manager(
        client=client,
        state=state,
        recovery_policy=replace(_fast_policy(), timeout=0.03),
    ) as manager:
        getting = asyncio.create_task(manager.get("owner"))
        await entered.wait()
        if cancel_waiter:
            getting.cancel()
        await settling.wait()
        if cancel_waiter:
            getting.cancel()
        next_get = asyncio.create_task(manager.get("owner"))
        try:
            done, _ = await asyncio.wait({getting, next_get}, timeout=0.02)
            assert not done
            assert initialized == 1 and connect.await_count == 1
        finally:
            release.set()
            expected = (
                asyncio.CancelledError
                if cancel_waiter
                else OpenSandboxBackendUnavailableError
            )
            with pytest.raises(expected):
                await getting
            assert (await next_get).id == "original"
        assert initialized == 2
        assert state.bindings == {"owner": "original"}
    assert first.closed and second.closed
    assert not first.killed and not second.killed


def test_recovery_policy_defaults_preserve_existing_instances() -> None:
    assert OpenSandboxRecoveryPolicy() == OpenSandboxRecoveryPolicy(
        max_attempts=3, initial_delay=0.5, max_delay=2, timeout=30, on_failure="raise"
    )


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_recovery_policy_rejects_invalid_attempt_counts(value: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        OpenSandboxRecoveryPolicy(max_attempts=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, True])
def test_recovery_policy_rejects_invalid_durations(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        OpenSandboxRecoveryPolicy(timeout=value)
