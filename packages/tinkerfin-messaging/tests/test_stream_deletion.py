"""Cross-backend stream deletion, fencing, and generation contracts."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

import tinkerfin_messaging as messaging_api
from tinkerfin_messaging import (
    BackendOwnershipLost,
    CodecMismatch,
    MemoryBackend,
    MessageEnvelope,
    Messaging,
    MessagingBackend,
    MessagingClosed,
    PreparedRun,
    StreamDeleted,
)


async def _prepare_memory(
    backend: MemoryBackend,
    *,
    run: str,
    codec: str = "test.bytes.v1",
) -> PreparedRun:
    return await backend.prepare(
        channel="events",
        stream="conversation-1",
        run=run,
        codec=codec,
        identity=f"identity:{run}",
        after=0,
        cancellable=True,
        recoverable=False,
    )


async def _prepare_backend(
    backend: MessagingBackend,
    *,
    stream: str = "conversation-1",
    run: str = "run-1",
    codec: str = "test.bytes.v1",
    cancellable: bool = False,
) -> PreparedRun:
    return await backend.prepare(
        channel="events",
        stream=stream,
        run=run,
        codec=codec,
        identity=f"identity:{run}",
        after=0,
        cancellable=cancellable,
        recoverable=False,
    )


async def _collect(
    iterator: AsyncIterator[MessageEnvelope],
) -> list[MessageEnvelope]:
    return [message async for message in iterator]


def test_public_stream_deletion_contract_is_exposed() -> None:
    delete_stream = MessagingBackend.delete_stream

    assert inspect.iscoroutinefunction(delete_stream)
    assert issubclass(messaging_api.StreamDeleted, messaging_api.MessagingError)
    assert issubclass(
        messaging_api.StreamDeleteConflict,
        messaging_api.MessagingError,
    )
    assert isinstance(MemoryBackend(), MessagingBackend)


async def test_message_channel_deletes_a_missing_stream_idempotently() -> None:
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events")

        await channel.delete_stream(stream="conversation-1")
        await channel.delete_stream(stream="conversation-1")


async def test_message_channel_validates_deletion_and_obeys_its_lifecycle() -> None:
    backend = MemoryBackend()
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events")

    with pytest.raises(ValueError):
        await channel.delete_stream(stream=" not-canonical ")

    await messaging.__aexit__(None, None, None)
    with pytest.raises(MessagingClosed):
        await channel.delete_stream(stream="conversation-1")


async def test_message_channel_propagates_active_stream_conflicts() -> None:
    backend = MemoryBackend()
    prepared = await _prepare_memory(backend, run="run-1")
    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events")

        with pytest.raises(messaging_api.StreamDeleteConflict):
            await channel.delete_stream(stream="conversation-1")

    await backend.finish(prepared.handle, status="completed")


async def test_memory_delete_rejects_an_active_producer_without_cancelling() -> None:
    backend = MemoryBackend()
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=False,
    )

    with pytest.raises(messaging_api.StreamDeleteConflict) as captured:
        await backend.delete_stream(channel="events", stream="conversation-1")

    assert captured.value.channel == "events"
    assert captured.value.stream == "conversation-1"
    assert captured.value.active_run == "run-1"
    assert await backend.request_cancel(prepared.handle) is True
    await backend.finish(prepared.handle, status="cancelled")


async def test_memory_delete_removes_a_terminal_stream_and_is_idempotent() -> None:
    backend = MemoryBackend()
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"persisted",
    )
    await backend.finish(prepared.handle, status="completed")

    await backend.delete_stream(channel="events", stream="conversation-1")
    await backend.delete_stream(channel="events", stream="conversation-1")

    assert await backend.latest_seq(channel="events", stream="conversation-1") == 0
    assert await backend.read(channel="events", stream="conversation-1", after=0) == ()


async def test_memory_rebuilds_a_deleted_name_as_an_isolated_generation() -> None:
    backend = MemoryBackend()
    old = await _prepare_memory(backend, run="run-old")
    await backend.append(
        old.handle,
        message_id="old-message",
        codec="test.bytes.v1",
        payload=b"old",
    )
    await backend.finish(old.handle, status="completed")
    await backend.delete_stream(channel="events", stream="conversation-1")

    new = await _prepare_memory(backend, run="run-new")
    committed = await backend.append(
        new.handle,
        message_id="new-message",
        codec="test.bytes.v1",
        payload=b"new",
    )
    await backend.finish(new.handle, status="completed")

    assert old.handle.generation is not None
    assert new.handle.generation == old.handle.generation + 1
    assert committed.seq == 1
    assert [
        message.payload
        for message in await backend.read(
            channel="events",
            stream="conversation-1",
            after=0,
        )
    ] == [b"new"]
    with pytest.raises(StreamDeleted):
        await _collect(backend.follow(old.handle, after=0))


async def test_old_handles_cannot_mutate_or_observe_a_rebuilt_stream(
    messaging_backend: MessagingBackend,
) -> None:
    old = await _prepare_backend(
        messaging_backend,
        run="run-old",
        cancellable=True,
    )
    await messaging_backend.finish(old.handle, status="completed")
    await messaging_backend.delete_stream(
        channel="events",
        stream="conversation-1",
    )
    await messaging_backend.delete_stream(
        channel="events",
        stream="conversation-1",
    )
    new = await _prepare_backend(messaging_backend, run="run-new")

    operations: tuple[Callable[[], Awaitable[object]], ...] = (
        lambda: messaging_backend.append(
            old.handle,
            message_id="stale-message",
            codec="test.bytes.v1",
            payload=b"stale",
        ),
        lambda: messaging_backend.begin_settlement(old.handle),
        lambda: messaging_backend.finish(old.handle, status="failed"),
        lambda: messaging_backend.request_cancel(old.handle),
        lambda: messaging_backend.wait_for_cancel(old.handle),
        lambda: messaging_backend.wait_finished(old.handle),
        lambda: messaging_backend.failure(old.handle),
        lambda: messaging_backend.renew(old.handle),
    )
    for operation in operations:
        with pytest.raises(StreamDeleted) as captured:
            await operation()
        assert captured.value.generation == old.handle.generation

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            stream="conversation-1",
        )
        == 0
    )
    await messaging_backend.finish(new.handle, status="completed")


async def test_delete_removes_an_empty_terminal_stream_across_backends(
    messaging_backend: MessagingBackend,
) -> None:
    prepared = await _prepare_backend(messaging_backend)
    await messaging_backend.finish(prepared.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        stream="conversation-1",
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            stream="conversation-1",
        )
        == 0
    )
    with pytest.raises(StreamDeleted):
        await _collect(messaging_backend.follow(prepared.handle, after=0))


async def test_memory_delete_wakes_a_waiting_follower_with_stream_deleted() -> None:
    backend = MemoryBackend()
    prepared = await _prepare_memory(backend, run="run-1")
    follower = backend.follow(prepared.handle, after=0)
    waiting = asyncio.ensure_future(anext(follower))
    await asyncio.sleep(0)

    await backend.finish(prepared.handle, status="completed")
    await backend.delete_stream(channel="events", stream="conversation-1")

    with pytest.raises(StreamDeleted):
        await waiting


async def test_memory_delete_preserves_the_channel_codec_binding() -> None:
    backend = MemoryBackend()
    prepared = await _prepare_memory(backend, run="run-1")
    await backend.finish(prepared.handle, status="completed")
    await backend.delete_stream(channel="events", stream="conversation-1")

    with pytest.raises(CodecMismatch):
        await _prepare_memory(
            backend,
            run="run-2",
            codec="test.other.v1",
        )


async def test_memory_unknown_owner_without_generation_is_not_stream_deleted() -> None:
    """Keep the existing ownership error distinct from cross-generation deletion."""

    backend = MemoryBackend()
    prepared = await _prepare_memory(backend, run="run-1")
    unknown_owner = messaging_api.BackendRunHandle(
        channel=prepared.handle.channel,
        stream=prepared.handle.stream,
        run=prepared.handle.run,
        owner_token="unknown",
        fence=prepared.handle.fence,
        generation=prepared.handle.generation,
    )

    with pytest.raises(BackendOwnershipLost):
        await backend.renew(unknown_owner)

    await backend.finish(prepared.handle, status="completed")


async def test_delete_rebuilds_an_isolated_generation_across_backends(
    messaging_backend: MessagingBackend,
) -> None:
    old = await _prepare_backend(messaging_backend, run="run-old")
    await messaging_backend.append(
        old.handle,
        message_id="old-message",
        codec="test.bytes.v1",
        payload=b"old",
    )
    await messaging_backend.finish(old.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        stream="conversation-1",
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            stream="conversation-1",
        )
        == 0
    )
    assert (
        await messaging_backend.read(
            channel="events",
            stream="conversation-1",
            after=0,
        )
        == ()
    )
    with pytest.raises(StreamDeleted):
        await _collect(messaging_backend.follow(old.handle, after=0))

    new = await _prepare_backend(messaging_backend, run="run-new")
    assert old.handle.generation is not None
    assert new.handle.generation == old.handle.generation + 1
    committed = await messaging_backend.append(
        new.handle,
        message_id="new-message",
        codec="test.bytes.v1",
        payload=b"new",
    )
    assert committed.seq == 1
    await messaging_backend.finish(new.handle, status="completed")


async def test_delete_rejects_an_active_producer_across_backends(
    messaging_backend: MessagingBackend,
) -> None:
    prepared = await _prepare_backend(
        messaging_backend,
        cancellable=True,
    )

    with pytest.raises(messaging_api.StreamDeleteConflict):
        await messaging_backend.delete_stream(
            channel="events",
            stream="conversation-1",
        )

    assert await messaging_backend.request_cancel(prepared.handle) is True
    await messaging_backend.finish(prepared.handle, status="cancelled")


async def test_delete_preserves_other_streams_and_channel_codec_across_backends(
    messaging_backend: MessagingBackend,
) -> None:
    deleted = await _prepare_backend(
        messaging_backend,
        stream="stream-deleted",
        run="run-deleted",
    )
    retained = await _prepare_backend(
        messaging_backend,
        stream="stream-retained",
        run="run-retained",
    )
    await messaging_backend.append(
        deleted.handle,
        message_id="deleted-message",
        codec="test.bytes.v1",
        payload=b"deleted",
    )
    await messaging_backend.append(
        retained.handle,
        message_id="retained-message",
        codec="test.bytes.v1",
        payload=b"retained",
    )
    await messaging_backend.finish(deleted.handle, status="completed")
    await messaging_backend.finish(retained.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        stream="stream-deleted",
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            stream="stream-deleted",
        )
        == 0
    )
    assert [
        message.payload
        for message in await messaging_backend.read(
            channel="events",
            stream="stream-retained",
            after=0,
        )
    ] == [b"retained"]
    with pytest.raises(CodecMismatch):
        await _prepare_backend(
            messaging_backend,
            stream="stream-new",
            run="run-new",
            codec="test.other.v1",
        )


async def test_delete_of_a_missing_name_does_not_consume_a_generation(
    messaging_backend: MessagingBackend,
) -> None:
    await messaging_backend.delete_stream(
        channel="events",
        stream="conversation-1",
    )
    await messaging_backend.delete_stream(
        channel="events",
        stream="conversation-1",
    )

    prepared = await _prepare_backend(messaging_backend)

    assert prepared.handle.generation == 1
    await messaging_backend.finish(prepared.handle, status="completed")
