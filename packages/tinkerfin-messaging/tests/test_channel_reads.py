"""Typed committed-event reads exposed by MessageChannel."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar, cast

import pytest

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    CodecMismatch,
    FiniteMessageSource,
    InvalidCursor,
    MemoryBackend,
    MessageChannel,
    Messaging,
    MessagingBackend,
    MessagingBackendProtocolError,
    MessagingClosed,
    MessagingErrorCode,
    RunNotFound,
    RunProducerFailed,
    RunStatus,
    StreamDeleted,
)


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(threadId=thread_id, runId=run_id)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _OtherTextCodec(_TextCodec):
    codec_id: ClassVar[str] = "test.other-text.v1"


class _FailingSource:
    def __init__(self) -> None:
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            yield "before-failure"
            raise RuntimeError("producer failed")

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        if self._iterator is not None:
            await self._iterator.aclose()


class _MissingRunStatusBackend(MessagingBackend):
    """Model an explicit backend subclass that omitted the new operation."""


class _InvalidRunStatusBackend(MemoryBackend):
    async def get_run_status(self, *, channel: str, identity: RunIdentity) -> RunStatus:
        del channel, identity
        return cast(RunStatus, "corrupted")


class _FalseyBackend(MemoryBackend):
    def __bool__(self) -> bool:
        return False


async def _commit(messaging: Messaging, *items: str) -> MessageChannel[str, str]:
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        FiniteMessageSource.from_events(items),
        identity=_identity(),
        after=0,
    )
    assert [message.data async for message in subscription] == list(items)
    return channel


async def test_channel_reads_typed_committed_pages_and_follows_one_run(
    messaging_backend: MessagingBackend,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "first", "second")

        assert await channel.latest_seq(identity=_identity()) == 2
        first = await channel.read(identity=_identity(), after=0, limit=1)
        second = await channel.read(identity=_identity(), after=1, limit=1000)
        following = await channel.follow(identity=_identity(), after=0)

        assert [(message.envelope.seq, message.data) for message in first] == [
            (1, "first")
        ]
        assert [(message.envelope.seq, message.data) for message in second] == [
            (2, "second")
        ]
        assert [message.data async for message in following] == ["first", "second"]


async def test_channel_empty_committed_stream_returns_zero_and_empty_page(
    messaging_backend: MessagingBackend,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        empty = _identity(thread_id="empty")
        assert await channel.latest_seq(identity=empty) == 0
        assert await channel.read(identity=empty, after=0, limit=100) == ()


async def test_channel_run_status_exposes_every_durable_state(
    messaging_backend: MessagingBackend,
) -> None:
    """Hosts can reconcile durable state without probing a blocking follower."""

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        missing = _identity(run_id="run-missing")
        with pytest.raises(RunNotFound):
            await channel.get_run_status(identity=missing)

        running = await messaging_backend.prepare(
            channel=channel.name,
            identity=_identity(run_id="run-running"),
            codec=_TextCodec.codec_id,
            after=0,
            cancellable=True,
            recoverable=False,
        )
        assert (
            await channel.get_run_status(identity=running.handle.identity) == "running"
        )
        await messaging_backend.finish(running.handle, status="completed")

        cancelling = await messaging_backend.prepare(
            channel=channel.name,
            identity=_identity(run_id="run-cancelling"),
            codec=_TextCodec.codec_id,
            after=0,
            cancellable=True,
            recoverable=False,
        )
        assert await messaging_backend.request_cancel(cancelling.handle) is True
        assert (
            await channel.get_run_status(identity=cancelling.handle.identity)
            == "cancel_requested"
        )
        await messaging_backend.finish(cancelling.handle, status="cancelled")

        for final_status in ("completed", "cancelled", "failed", "owner_lost"):
            prepared = await messaging_backend.prepare(
                channel=channel.name,
                identity=_identity(run_id=f"run-{final_status}"),
                codec=_TextCodec.codec_id,
                after=0,
                cancellable=True,
                recoverable=False,
            )
            failure = (
                RuntimeError(final_status)
                if final_status in {"failed", "owner_lost"}
                else None
            )
            await messaging_backend.finish(
                prepared.handle,
                status=final_status,
                error=failure,
            )
            assert (
                await channel.get_run_status(identity=prepared.handle.identity)
                == final_status
            )


async def test_channel_run_status_rejects_calls_after_messaging_closes() -> None:
    messaging = Messaging(backend=MemoryBackend())
    async with messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

    with pytest.raises(MessagingClosed):
        await channel.get_run_status(identity=_identity())


def test_messaging_rejects_a_backend_missing_the_status_contract() -> None:
    """Explicit and structural backends must implement the complete contract."""

    assert inspect.isabstract(_MissingRunStatusBackend)
    with pytest.raises(TypeError, match="backend must implement MessagingBackend"):
        Messaging(backend=cast(MessagingBackend, object()))


def test_messaging_keeps_a_valid_falsey_backend() -> None:
    """Backend ownership is based on explicit presence rather than truthiness."""

    backend = _FalseyBackend()

    assert Messaging(backend=backend).backend is backend


@pytest.mark.parametrize(
    "backend",
    [_InvalidRunStatusBackend()],
    ids=["invalid-status"],
)
async def test_channel_run_status_rejects_invalid_backend_results(
    backend: MessagingBackend,
) -> None:
    """Replaceable backends cannot silently violate the public status contract."""

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(MessagingBackendProtocolError) as caught:
            await channel.get_run_status(identity=_identity())

    assert caught.value.code is MessagingErrorCode.BACKEND_PROTOCOL_ERROR
    assert caught.value.diagnostic_context["operation"] == "get_run_status"


async def test_channel_read_and_follow_reject_cursors_beyond_thread_tail(
    messaging_backend: MessagingBackend,
) -> None:
    """Direct read and follow must enforce the same strict cursor contract."""

    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "first")

        with pytest.raises(InvalidCursor) as read_error:
            await channel.read(identity=_identity(), after=999)
        with pytest.raises(InvalidCursor) as follow_error:
            await channel.follow(identity=_identity(), after=999)

        assert read_error.value.latest == 1
        assert follow_error.value.latest == 1


@pytest.mark.parametrize(
    ("after", "limit", "error", "message"),
    [
        (-1, 1, ValueError, "greater than or equal to zero"),
        (True, 1, TypeError, "after must be an integer"),
        (0, 0, ValueError, "between 1 and 1000"),
        (0, 1001, ValueError, "between 1 and 1000"),
        (0, True, TypeError, "limit must be an integer"),
    ],
)
async def test_channel_read_validates_page_bounds(
    after: int,
    limit: int,
    error: type[Exception],
    message: str,
) -> None:
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(error, match=message):
            await channel.read(identity=_identity(), after=after, limit=limit)


async def test_channel_read_rejects_persisted_codec_mismatch() -> None:
    async with Messaging(backend=MemoryBackend()) as messaging:
        await _commit(messaging, "first")
        mismatched = messaging.channel(name="events", codec=_OtherTextCodec())

        with pytest.raises(CodecMismatch):
            await mismatched.read(identity=_identity(), after=0, limit=10)


async def test_channel_follow_preserves_unknown_run_failure_and_can_close_early() -> (
    None
):
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = await _commit(messaging, "first", "second")
        with pytest.raises(RunNotFound):
            await channel.follow(identity=_identity(run_id="missing"), after=0)

        following = await channel.follow(identity=_identity(), after=0)
        assert (await anext(aiter(following))).data == "first"
        await following.aclose()


async def test_channel_follow_preserves_producer_failure_after_committed_events() -> (
    None
):
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        owner = await channel.wrap(
            _FailingSource(),
            identity=_identity(run_id="run-failed"),
            after=0,
        )

        owner_iterator = aiter(owner)
        assert (await anext(owner_iterator)).data == "before-failure"
        with pytest.raises(RunProducerFailed):
            await anext(owner_iterator)

        following = await channel.follow(
            identity=_identity(run_id="run-failed"),
            after=0,
        )
        following_iterator = aiter(following)
        assert (await anext(following_iterator)).data == "before-failure"
        with pytest.raises(RunProducerFailed):
            await anext(following_iterator)


async def test_channel_follow_keeps_its_committed_terminal_snapshot_during_deletion(
    messaging_backend: MessagingBackend,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "first", "second")
        following = await channel.follow(identity=_identity(), after=0)
        iterator = aiter(following)

        assert (await anext(iterator)).data == "first"
        await channel.delete_stream(identity=_identity())

        assert (await anext(iterator)).data == "second"
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)


async def test_channel_follow_binds_generation_before_first_pull(
    messaging_backend: MessagingBackend,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = await _commit(messaging, "old")
        reader = messaging.channel(name="events", codec=_TextCodec())
        stale = await reader.follow(identity=_identity(), after=0)

        await channel.delete_stream(identity=_identity())
        replacement = await channel.wrap(
            FiniteMessageSource.from_events(("new",)),
            identity=_identity(),
            after=0,
        )
        assert [message.data async for message in replacement] == ["new"]

        with pytest.raises(StreamDeleted):
            await anext(aiter(stale))
