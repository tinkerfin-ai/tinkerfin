"""Terminal replay retention shared by Messaging backend implementations."""

from __future__ import annotations

import asyncio

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    MemoryBackend,
    MessagingRetentionPolicy,
    StreamDeleted,
    StreamExpired,
)
from tinkerfin_messaging._messaging_ledger import _MessagingLedger


def _identity(run_id: str = "run-retention") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="thread-retention", run_id=run_id)


async def _start(
    backend: _MessagingLedger,
    run_id: str,
    *,
    after: int | None = None,
):
    return await backend.prepare(
        channel="events",
        identity=_identity(run_id),
        codec="tests.json",
        after=after,
        cancellable=False,
        recoverable=False,
    )


async def _finish(backend: _MessagingLedger, prepared) -> None:
    await backend.begin_settlement(prepared.handle)
    await backend.finish(prepared.handle, status="completed")


def test_retention_policy_is_explicit_and_immutable() -> None:
    disabled = MessagingRetentionPolicy.disabled()
    enabled = MessagingRetentionPolicy.expire_after(30)

    assert disabled.enabled is False
    assert disabled.terminal_ttl_seconds is None
    assert enabled.enabled is True
    assert enabled.terminal_ttl_seconds == 30.0
    with pytest.raises((TypeError, ValueError)):
        MessagingRetentionPolicy.expire_after(0)
    with pytest.raises((TypeError, ValueError)):
        MessagingRetentionPolicy.expire_after(float("nan"))


async def test_memory_active_run_never_expires() -> None:
    backend = _MessagingLedger(
        MemoryBackend(retention_policy=MessagingRetentionPolicy.expire_after(0.02))
    )
    prepared = await _start(backend, "active", after=0)

    await asyncio.sleep(0.04)
    committed = await backend.append(
        prepared.handle,
        message_id="active-message",
        codec="tests.json",
        payload=b"{}",
    )

    assert committed.seq == 1
    assert (
        await backend.get_run_status(
            channel="events",
            identity=_identity("active"),
        )
        == "running"
    )
    await _finish(backend, prepared)


async def test_memory_new_run_before_deadline_clears_terminal_timer() -> None:
    backend = _MessagingLedger(
        MemoryBackend(retention_policy=MessagingRetentionPolicy.expire_after(0.08))
    )
    first = await _start(backend, "first", after=0)
    await backend.append(
        first.handle,
        message_id="first-message",
        codec="tests.json",
        payload=b"first",
    )
    await _finish(backend, first)
    await asyncio.sleep(0.04)

    second = await _start(backend, "second")
    assert second.handle.generation == first.handle.generation
    await asyncio.sleep(0.06)

    replay = await backend.read(
        channel="events",
        identity=_identity("second"),
    )
    assert tuple(item.message_id for item in replay) == ("first-message",)
    await _finish(backend, second)


async def test_memory_expiry_uses_new_generation_and_preserves_tombstone() -> None:
    backend = _MessagingLedger(
        MemoryBackend(retention_policy=MessagingRetentionPolicy.expire_after(0.02))
    )
    first = await _start(backend, "first", after=0)
    await backend.append(
        first.handle,
        message_id="first-message",
        codec="tests.json",
        payload=b"first",
    )
    await _finish(backend, first)
    await asyncio.sleep(0.04)

    with pytest.raises(StreamExpired) as expired:
        await backend.read(
            channel="events",
            identity=_identity("first"),
            after=0,
        )
    assert expired.value.generation == first.handle.generation
    assert expired.value.code.value == "messaging.stream_expired"
    with pytest.raises(StreamExpired):
        await _start(backend, "second", after=1)

    second = await _start(backend, "second", after=0)
    assert first.handle.generation is not None
    assert second.handle.generation == first.handle.generation + 1
    assert (
        await backend.latest_seq(
            channel="events",
            identity=_identity("second"),
        )
        == 0
    )
    with pytest.raises(StreamExpired):
        await anext(backend.follow(first.handle, after=0))

    await _finish(backend, second)
    await backend.delete_stream(channel="events", identity=_identity("second"))
    with pytest.raises(StreamDeleted):
        await anext(backend.follow(second.handle, after=0))


async def test_memory_disabled_retention_requires_explicit_delete() -> None:
    backend = _MessagingLedger(
        MemoryBackend(retention_policy=MessagingRetentionPolicy.disabled())
    )
    prepared = await _start(backend, "kept", after=0)
    await backend.append(
        prepared.handle,
        message_id="kept-message",
        codec="tests.json",
        payload=b"kept",
    )
    await _finish(backend, prepared)
    await asyncio.sleep(0.03)

    assert (
        await backend.latest_seq(
            channel="events",
            identity=_identity("kept"),
        )
        == 1
    )
