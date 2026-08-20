"""Typed committed-event reads exposed by MessageChannel."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar

import pytest

from tinkerfin import Identity
from tinkerfin_messaging import (
    CodecMismatch,
    FiniteMessageSource,
    MemoryBackend,
    MessageChannel,
    Messaging,
    MessagingBackend,
    RunNotFound,
    RunProducerFailed,
    StreamDeleted,
)


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> Identity:
    return Identity(threadId=thread_id, runId=run_id)


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
