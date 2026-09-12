"""Cross-backend stream deletion, fencing, and generation contracts."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from backend_harness import MessagingBackendHarness

import tinkerfin_messaging as messaging_api
from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    CodecMismatch,
    MemoryBackend,
    MessageEnvelope,
    Messaging,
    MessagingClosed,
    RunNotFound,
    StreamDeleted,
)
from tinkerfin_messaging._messaging_ledger import BackendRunHandle, PreparedRun
from tinkerfin_messaging.backend_contract import (
    MessagingBackend,
    MessagingStateQuery,
    MessagingTransition,
    MessagingTransitionResult,
    StreamGenerationPurge,
    StreamGenerationPurgeResult,
)


def _identity(
    *,
    thread_id: str = "conversation-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


async def _prepare_memory(
    backend: MessagingBackendHarness,
    *,
    identity: RunIdentity,
    codec: str = "test.bytes.v1",
) -> PreparedRun:
    return await backend.prepare(
        channel="events",
        identity=identity,
        codec=codec,
        after=0,
        cancellable=True,
        recoverable=False,
    )


async def _prepare_backend(
    backend: MessagingBackendHarness,
    *,
    identity: RunIdentity | None = None,
    codec: str = "test.bytes.v1",
    cancellable: bool = False,
) -> PreparedRun:
    resolved_identity = identity or _identity()
    return await backend.prepare(
        channel="events",
        identity=resolved_identity,
        codec=codec,
        after=0,
        cancellable=cancellable,
        recoverable=False,
    )


async def _collect(
    iterator: AsyncIterator[MessageEnvelope],
) -> list[MessageEnvelope]:
    return [message async for message in iterator]


class _ConcurrentCleanupMemoryBackend(MemoryBackend):
    """Hold the first purge until two cleanup transitions have sealed a generation."""

    def __init__(self) -> None:
        super().__init__()
        self.cleanup_begins = 0
        self.both_cleanups_begun = asyncio.Event()

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        result = await super().commit_messaging_transition(transition)
        if transition.kind == "begin_generation_cleanup":
            self.cleanup_begins += 1
            if self.cleanup_begins == 2:
                self.both_cleanups_begun.set()
        return result

    async def purge_stream_generation(
        self,
        purge: StreamGenerationPurge,
    ) -> StreamGenerationPurgeResult:
        await self.both_cleanups_begun.wait()
        return await super().purge_stream_generation(purge)


def test_public_stream_deletion_contract_is_exposed() -> None:
    purge_generation = MessagingBackend.purge_stream_generation

    assert inspect.iscoroutinefunction(purge_generation)
    assert inspect.iscoroutinefunction(MessagingBackend.commit_messaging_transition)
    assert inspect.iscoroutinefunction(messaging_api.MessageChannel.follow)
    assert issubclass(messaging_api.StreamDeleted, messaging_api.MessagingError)
    assert issubclass(
        messaging_api.StreamDeleteConflict,
        messaging_api.MessagingError,
    )
    assert isinstance(MemoryBackend(), MessagingBackend)


async def test_message_channel_deletes_a_missing_stream_idempotently() -> None:
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events")

        await channel.delete_stream(identity=_identity())
        await channel.delete_stream(identity=_identity())


async def test_message_channel_validates_deletion_and_obeys_its_lifecycle() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events")

    with pytest.raises(ValueError):
        await channel.delete_stream(
            identity=RunIdentity.model_construct(
                thread_id=" not-canonical ",
                run_id="run-1",
            )
        )

    await messaging.__aexit__(None, None, None)
    with pytest.raises(MessagingClosed):
        await channel.delete_stream(identity=_identity())


async def test_message_channel_propagates_active_stream_conflicts() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    prepared = await _prepare_memory(backend, identity=_identity())
    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events")

        with pytest.raises(messaging_api.StreamDeleteConflict):
            await channel.delete_stream(identity=_identity())

    await backend.finish(prepared.handle, status="completed")


async def test_memory_delete_rejects_an_active_producer_without_cancelling() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=True,
        recoverable=False,
    )

    with pytest.raises(messaging_api.StreamDeleteConflict) as captured:
        await backend.delete_stream(channel="events", identity=_identity())

    assert captured.value.channel == "events"
    assert captured.value.identity == _identity()
    assert captured.value.active_identity == _identity()
    assert await backend.request_cancel(prepared.handle) is True
    await backend.finish(prepared.handle, status="cancelled")


async def test_memory_delete_removes_a_terminal_stream_and_is_idempotent() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
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

    await backend.delete_stream(channel="events", identity=_identity())
    await backend.delete_stream(channel="events", identity=_identity())

    assert await backend.latest_seq(channel="events", identity=_identity()) == 0
    assert await backend.read(channel="events", identity=_identity(), after=0) == ()


async def test_concurrent_memory_deletes_finish_the_same_generation_idempotently() -> (
    None
):
    storage_backend = _ConcurrentCleanupMemoryBackend()
    first = MessagingBackendHarness(storage_backend)
    second = MessagingBackendHarness(storage_backend)
    prepared = await _prepare_memory(first, identity=_identity())
    await first.finish(prepared.handle, status="completed")

    await asyncio.gather(
        first.delete_stream(channel="events", identity=_identity()),
        second.delete_stream(channel="events", identity=_identity()),
    )

    assert storage_backend.cleanup_begins == 2
    assert await first.latest_seq(channel="events", identity=_identity()) == 0


async def test_memory_rebuilds_a_deleted_name_as_an_isolated_generation() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    old = await _prepare_memory(backend, identity=_identity(run_id="run-old"))
    await backend.append(
        old.handle,
        message_id="old-message",
        codec="test.bytes.v1",
        payload=b"old",
    )
    await backend.finish(old.handle, status="completed")
    await backend.delete_stream(channel="events", identity=_identity())

    new = await _prepare_memory(backend, identity=_identity(run_id="run-new"))
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
            identity=_identity(),
            after=0,
        )
    ] == [b"new"]
    with pytest.raises(StreamDeleted):
        await _collect(backend.follow(old.handle, after=0))


async def test_old_handles_cannot_mutate_or_observe_a_rebuilt_stream(
    messaging_backend: MessagingBackendHarness,
) -> None:
    old = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-old"),
        cancellable=True,
    )
    await messaging_backend.finish(old.handle, status="completed")
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )
    new = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-new"),
    )

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
            identity=_identity(),
        )
        == 0
    )
    await messaging_backend.finish(new.handle, status="completed")


async def test_delete_removes_an_empty_terminal_stream_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await _prepare_backend(messaging_backend)
    await messaging_backend.finish(prepared.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )

    with pytest.raises(RunNotFound):
        await messaging_backend.get_run_status(
            channel="events",
            identity=_identity(),
        )
    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(),
        )
        == 0
    )
    with pytest.raises(StreamDeleted):
        await _collect(messaging_backend.follow(prepared.handle, after=0))


async def test_exact_generation_state_allows_a_missing_target_run_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await _prepare_backend(messaging_backend)
    assert prepared.handle.generation is not None

    snapshot = await messaging_backend.load_messaging_state(
        MessagingStateQuery(
            channel="events",
            identity=_identity(run_id="missing-run"),
            generation=prepared.handle.generation,
        )
    )

    assert snapshot.stream is not None
    assert snapshot.stream.generation == prepared.handle.generation
    assert snapshot.target_run is None
    await messaging_backend.finish(prepared.handle, status="completed")


async def test_memory_delete_wakes_a_waiting_follower_with_stream_deleted() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    prepared = await _prepare_memory(backend, identity=_identity())
    follower = backend.follow(prepared.handle, after=0)
    waiting = asyncio.ensure_future(anext(follower))
    await asyncio.sleep(0)

    await backend.finish(prepared.handle, status="completed")
    await backend.delete_stream(channel="events", identity=_identity())

    with pytest.raises(StreamDeleted):
        await waiting


async def test_memory_delete_preserves_the_channel_codec_binding() -> None:
    backend = MessagingBackendHarness(MemoryBackend())
    prepared = await _prepare_memory(backend, identity=_identity())
    await backend.finish(prepared.handle, status="completed")
    await backend.delete_stream(channel="events", identity=_identity())

    with pytest.raises(CodecMismatch):
        await _prepare_memory(
            backend,
            identity=_identity(run_id="run-2"),
            codec="test.other.v1",
        )


async def test_memory_unknown_owner_is_rejected_without_stream_deletion() -> None:
    """Keep ownership rejection distinct from cross-generation deletion."""

    backend = MessagingBackendHarness(MemoryBackend())
    prepared = await _prepare_memory(backend, identity=_identity())
    unknown_owner = BackendRunHandle(
        channel=prepared.handle.channel,
        identity=prepared.handle.identity,
        owner_token="unknown",
        fence=prepared.handle.fence,
        generation=prepared.handle.generation,
    )

    assert await backend.renew(unknown_owner) is False

    await backend.finish(prepared.handle, status="completed")


async def test_delete_rebuilds_an_isolated_generation_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    old = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-old"),
    )
    await messaging_backend.append(
        old.handle,
        message_id="old-message",
        codec="test.bytes.v1",
        payload=b"old",
    )
    await messaging_backend.finish(old.handle, status="completed")

    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(),
        )
        == 0
    )
    assert (
        await messaging_backend.read(
            channel="events",
            identity=_identity(),
            after=0,
        )
        == ()
    )
    with pytest.raises(StreamDeleted):
        await _collect(messaging_backend.follow(old.handle, after=0))

    new = await _prepare_backend(
        messaging_backend,
        identity=_identity(run_id="run-new"),
    )
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
    messaging_backend: MessagingBackendHarness,
) -> None:
    prepared = await _prepare_backend(
        messaging_backend,
        cancellable=True,
    )

    with pytest.raises(messaging_api.StreamDeleteConflict):
        await messaging_backend.delete_stream(
            channel="events",
            identity=_identity(),
        )

    assert await messaging_backend.request_cancel(prepared.handle) is True
    await messaging_backend.finish(prepared.handle, status="cancelled")


async def test_delete_preserves_other_streams_and_channel_codec_across_backends(
    messaging_backend: MessagingBackendHarness,
) -> None:
    deleted = await _prepare_backend(
        messaging_backend,
        identity=_identity(thread_id="stream-deleted", run_id="run-deleted"),
    )
    retained = await _prepare_backend(
        messaging_backend,
        identity=_identity(thread_id="stream-retained", run_id="run-retained"),
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
        identity=_identity(thread_id="stream-deleted", run_id="run-deleted"),
    )

    assert (
        await messaging_backend.latest_seq(
            channel="events",
            identity=_identity(thread_id="stream-deleted", run_id="run-deleted"),
        )
        == 0
    )
    assert [
        message.payload
        for message in await messaging_backend.read(
            channel="events",
            identity=_identity(thread_id="stream-retained", run_id="run-retained"),
            after=0,
        )
    ] == [b"retained"]
    with pytest.raises(CodecMismatch):
        await _prepare_backend(
            messaging_backend,
            identity=_identity(thread_id="stream-new", run_id="run-new"),
            codec="test.other.v1",
        )


async def test_delete_of_a_missing_name_does_not_consume_a_generation(
    messaging_backend: MessagingBackendHarness,
) -> None:
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )
    await messaging_backend.delete_stream(
        channel="events",
        identity=_identity(),
    )

    prepared = await _prepare_backend(messaging_backend)

    assert prepared.handle.generation == 1
    await messaging_backend.finish(prepared.handle, status="completed")
