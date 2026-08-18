"""Async preflight and source-ownership contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import ClassVar, cast

import pytest

from tinkerfin_messaging import (
    InvalidCursor,
    MessageSubscription,
    Messaging,
    MessagingBackend,
    RunAlreadyActive,
    RunIdentityConflict,
    SseRenderingUnsupported,
)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


@dataclass(frozen=True, slots=True)
class _TextSseRenderer:
    def render(self, *, seq: int, payload: str) -> bytes:
        return f"id: {seq}\ndata: {payload}\n\n".encode()


class _ControlledSource:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
    ) -> None:
        self._items = items
        self._release = release
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        if self._iterator is not None:
            raise RuntimeError("source is single-use")

        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self._items:
                yield item
            if self._release is not None:
                await self._release.wait()

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        if self._release is not None:
            self._release.set()
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


async def _collect_data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def test_invalid_cursor_is_rejected_during_wrap_and_closes_source(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(InvalidCursor) as captured:
            await channel.wrap(
                source,
                stream="conversation-1",
                run="run-1",
                after=1,
            )

    assert captured.value.after == 1
    assert captured.value.latest == 0
    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_boolean_cursor_is_rejected_during_wrap_and_closes_source(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(TypeError, match="after must be an integer or None"):
            await channel.wrap(
                source,
                stream="conversation-1",
                run="run-1",
                after=True,
            )

    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_sse_requires_a_renderer_without_affecting_async_replay(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("message")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )

        with pytest.raises(SseRenderingUnsupported, match="no SSE renderer"):
            subscription.sse()
        assert await _collect_data(subscription) == ["message"]


async def test_validate_cursor_checks_tail_without_claiming_a_run(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("message")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        await channel.validate_cursor(stream="conversation-1", after=0)
        with pytest.raises(InvalidCursor) as captured:
            await channel.validate_cursor(stream="conversation-1", after=1)
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _collect_data(subscription) == ["message"]

    assert captured.value.after == 1
    assert captured.value.latest == 0
    assert source.close_calls == 1


async def test_invalid_stream_closes_the_unclaimed_source(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(ValueError, match="stream must be non-blank"):
            await channel.wrap(
                source,
                stream="",
                run="run-1",
                after=0,
            )

    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_same_run_attaches_and_closes_the_unused_candidate_source(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    owner = _ControlledSource("first", release=release)
    candidate = _ControlledSource("must-not-run")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        first = await channel.wrap(
            owner,
            stream="conversation-1",
            run="run-1",
            after=0,
            attach_identity={"input": "hello"},
        )
        await asyncio.wait_for(owner.started.wait(), timeout=1)

        second = await channel.wrap(
            candidate,
            stream="conversation-1",
            run="run-1",
            after=0,
            attach_identity={"input": "hello"},
        )

        release.set()
        first_data, second_data = await asyncio.gather(
            _collect_data(first),
            _collect_data(second),
        )

    assert first_data == ["first"]
    assert second_data == ["first"]
    assert candidate.close_calls == 1
    assert not candidate.started.is_set()
    assert owner.close_calls == 1


async def test_wrap_rejects_identity_and_active_run_conflicts_before_returning(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    owner = _ControlledSource("first", release=release)
    identity_conflict = _ControlledSource("identity-conflict")
    active_conflict = _ControlledSource("active-conflict")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            owner,
            stream="conversation-1",
            run="run-1",
            after=0,
            attach_identity={"input": "hello"},
        )
        await asyncio.wait_for(owner.started.wait(), timeout=1)

        with pytest.raises(RunIdentityConflict):
            await channel.wrap(
                identity_conflict,
                stream="conversation-1",
                run="run-1",
                after=0,
                attach_identity={"input": "different"},
            )

        with pytest.raises(RunAlreadyActive):
            await channel.wrap(
                active_conflict,
                stream="conversation-1",
                run="run-2",
                after=0,
            )

        release.set()
        await _collect_data(subscription)

    assert identity_conflict.close_calls == 1
    assert active_conflict.close_calls == 1
    assert not identity_conflict.started.is_set()
    assert not active_conflict.started.is_set()


async def test_sse_renderer_uses_the_durable_sequence(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("one", "two")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )

        frames = [frame async for frame in subscription.sse()]

    assert frames == [
        b"id: 1\ndata: one\n\n",
        b"id: 2\ndata: two\n\n",
    ]


async def test_channel_sse_resolves_cursor_callback_once(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("one", "two")
    resolver_calls = 0

    def resolve_after() -> int:
        nonlocal resolver_calls
        resolver_calls += 1
        return 0

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        body = await channel.sse(
            source,
            stream="conversation-1",
            run="run-1",
            after=resolve_after,
        )
        frames = [frame async for frame in body]

    assert resolver_calls == 1
    assert frames == [
        b"id: 1\ndata: one\n\n",
        b"id: 2\ndata: two\n\n",
    ]
    assert source.close_calls == 1


async def test_channel_sse_cursor_callback_can_follow_from_current_tail(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("message")
    resolver_calls = 0

    def resolve_after() -> None:
        nonlocal resolver_calls
        resolver_calls += 1

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        body = await channel.sse(
            source,
            stream="conversation-1",
            run="run-1",
            after=resolve_after,
        )
        frames = [frame async for frame in body]

    assert resolver_calls == 1
    assert frames == [b"id: 1\ndata: message\n\n"]
    assert source.close_calls == 1


async def test_channel_sse_rejects_invalid_resolved_cursor_before_iteration(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")
    invalid_resolver = cast(Callable[[], int | None], lambda: "invalid")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        with pytest.raises(TypeError, match="after must be an integer or None"):
            await channel.sse(
                source,
                stream="conversation-1",
                run="run-1",
                after=invalid_resolver,
            )

    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_channel_sse_closes_source_when_cursor_callback_fails(
    messaging_backend: MessagingBackend,
) -> None:
    source = _ControlledSource("never-consumed")
    resolver_calls = 0

    def resolve_after() -> int:
        nonlocal resolver_calls
        resolver_calls += 1
        raise RuntimeError("cursor lookup failed")

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(
            name="events",
            codec=_TextCodec(),
            renderer=_TextSseRenderer(),
        )
        with pytest.raises(RuntimeError, match="cursor lookup failed"):
            await channel.sse(
                source,
                stream="conversation-1",
                run="run-1",
                after=resolve_after,
            )

    assert resolver_calls == 1
    assert source.close_calls == 1
    assert not source.started.is_set()
