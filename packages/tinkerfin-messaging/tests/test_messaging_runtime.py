"""Producer independence, replay cursor, and run-boundary contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar

import pytest

from tinkerfin_messaging import (
    DecodedMessage,
    DeferredMessageSource,
    MessageSourceBinding,
    MessageSubscription,
    Messaging,
    MessagingBackend,
    RunProducerFailed,
)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _Source:
    def __init__(
        self,
        before_release: tuple[str, ...],
        *,
        release: asyncio.Event | None = None,
        after_release: tuple[str, ...] = (),
        error: BaseException | None = None,
    ) -> None:
        self.before_release = before_release
        self.release = release
        self.after_release = after_release
        self.error = error
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        if self._iterator is not None:
            raise RuntimeError("source is single-use")

        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for value in self.before_release:
                yield value
            if self.release is not None:
                await self.release.wait()
            for value in self.after_release:
                yield value
            if self.error is not None:
                raise self.error

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def test_detach_does_not_stop_the_producer_and_cursor_reconnects(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    source = _Source(
        ("first",),
        release=release,
        after_release=("second",),
    )

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        delivery = aiter(first)
        assert (await anext(delivery)).data == "first"

        await first.aclose()
        assert not source.closed.is_set()
        release.set()
        await asyncio.wait_for(source.closed.wait(), timeout=1)

        unused = _Source(("must-not-run",))
        resumed = await channel.wrap(
            unused,
            stream="conversation-1",
            run="run-1",
            after=1,
        )
        assert await _data(resumed) == ["second"]

    assert source.close_calls == 1
    assert unused.close_calls == 1
    assert not unused.started.is_set()


async def test_completed_run_attachment_never_opens_deferred_source(
    messaging_backend: MessagingBackend,
) -> None:
    """A completed-run replay must not build the unused replacement producer."""

    open_calls = 0

    async def open_source() -> MessageSourceBinding[str]:
        nonlocal open_calls
        open_calls += 1
        return MessageSourceBinding(source=_Source(("must-not-run",)))

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first = await channel.wrap(
            _Source(("persisted",)),
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _data(first) == ["persisted"]

        replay = await channel.wrap(
            DeferredMessageSource(open_source, cancellable=False),
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _data(replay) == ["persisted"]

    assert open_calls == 0


async def test_none_cursor_captures_the_tail_before_starting_a_new_run(
    messaging_backend: MessagingBackend,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first = await channel.wrap(
            _Source(("old",)),
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _data(first) == ["old"]

        second = await channel.wrap(
            _Source(("new",)),
            stream="conversation-1",
            run="run-2",
            after=None,
        )

        assert await _data(second) == ["new"]


async def test_maximum_length_run_derives_stable_bounded_message_ids(
    messaging_backend: MessagingBackend,
) -> None:
    run = "r" * 1024

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            _Source(("first", "second")),
            stream="conversation-1",
            run=run,
            after=0,
        )

        messages = [message async for message in subscription]

    assert [message.data for message in messages] == ["first", "second"]
    message_ids = [message.envelope.message_id for message in messages]
    assert message_ids == [
        "sha256:fdbc7d6838188f3de19b6ab9554c45e8890abdb206d9c5807d3fe1555c6f6147",
        "sha256:05bff69bee8dc4901acd0ab2fbc6e2627f7d2318e0ec37c3e8cd7e9d0cb21e51",
    ]


async def test_explicit_zero_replays_history_until_the_requested_run_end(
    messaging_backend: MessagingBackend,
) -> None:
    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first = await channel.wrap(
            _Source(("old",)),
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _data(first) == ["old"]
        second = await channel.wrap(
            _Source(("new",)),
            stream="conversation-1",
            run="run-2",
            after=0,
        )
        assert await _data(second) == ["old", "new"]

        unused = _Source(("unused",))
        replay_old = await channel.wrap(
            unused,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _data(replay_old) == ["old"]


async def test_payload_that_looks_terminal_does_not_finish_the_producer(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    source = _Source(
        ('{"type":"RUN_FINISHED"}',),
        release=release,
        after_release=("after-terminal-looking-payload",),
    )

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == '{"type":"RUN_FINISHED"}'

        async def read_next() -> DecodedMessage[str]:
            return await anext(delivery)

        next_message = asyncio.create_task(read_next())
        await asyncio.sleep(0)
        assert not next_message.done()
        release.set()
        assert (await asyncio.wait_for(next_message, timeout=1)).data == (
            "after-terminal-looking-payload"
        )
        with pytest.raises(StopAsyncIteration):
            await anext(delivery)


async def test_source_failure_drains_the_committed_prefix_then_propagates(
    messaging_backend: MessagingBackend,
) -> None:
    cause = RuntimeError("source failed")
    source = _Source(("committed",), error=cause)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        delivery = aiter(subscription)

        assert (await anext(delivery)).data == "committed"
        with pytest.raises(RunProducerFailed) as captured:
            await anext(delivery)

    assert captured.value.cause is not None
    assert str(cause) in str(captured.value.cause)
    assert source.close_calls == 1


async def test_different_streams_can_produce_concurrently(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    first = _Source(("first",), release=release)
    second = _Source(("second",), release=release)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first_subscription = await channel.wrap(
            first,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        second_subscription = await channel.wrap(
            second,
            stream="conversation-2",
            run="run-2",
            after=0,
        )
        await asyncio.wait_for(first.started.wait(), timeout=1)
        await asyncio.wait_for(second.started.wait(), timeout=1)
        release.set()

        assert await _data(first_subscription) == ["first"]
        assert await _data(second_subscription) == ["second"]
