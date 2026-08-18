"""Recoverable factory, stable ID, and lifecycle contracts across backends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar

import pytest

from tinkerfin_messaging import (
    BackendOwnershipLost,
    BackendRunHandle,
    CancelContext,
    MemoryBackend,
    MessageSubscription,
    Messaging,
    MessagingBackend,
    RecoverableMessage,
    RecoveryCheckpoint,
    RunProducerFailed,
)


class _TextCodec:
    codec_id: ClassVar[str] = "test.recoverable-text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _Source:
    def __init__(
        self,
        *messages: RecoverableMessage[str],
        release: asyncio.Event | None = None,
    ) -> None:
        self.messages = messages
        self.release = release
        self.started = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
        async def iterate() -> AsyncGenerator[RecoverableMessage[str], None]:
            self.started.set()
            for message in self.messages:
                yield message
            if self.release is not None:
                await self.release.wait()

        return iterate()

    async def aclose(self) -> None:
        self.close_calls += 1


class _Factory:
    def __init__(
        self,
        source: _Source | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.source = source
        self.error = error
        self.checkpoints: list[RecoveryCheckpoint | None] = []

    async def open(self, checkpoint: RecoveryCheckpoint | None) -> _Source:
        self.checkpoints.append(checkpoint)
        if self.error is not None:
            raise self.error
        if self.source is None:
            raise AssertionError("test factory has no source")
        return self.source


class _ConcurrentOpenFailureFactory:
    def __init__(self, barrier: asyncio.Barrier) -> None:
        self._barrier = barrier

    async def open(self, checkpoint: RecoveryCheckpoint | None) -> _Source:
        assert checkpoint is None
        await self._barrier.wait()
        raise RuntimeError("source reconstruction failed")


class _ConcurrentRenewalFailureBackend(MemoryBackend):
    def __init__(self, barrier: asyncio.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    @property
    def lease_renew_interval(self) -> float:
        return 0.001

    async def renew(self, handle: BackendRunHandle) -> bool:
        del handle
        await self._barrier.wait()
        raise BackendOwnershipLost("recoverable source owner lease was lost")


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def test_recoverable_owner_uses_stable_id_and_attach_does_not_open_factory(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    source = _Source(
        RecoverableMessage(
            message_id="stable-message-1",
            data="one",
            checkpoint=RecoveryCheckpoint(
                position=b"1",
                last_message_id="stable-message-1",
            ),
        ),
        release=release,
    )
    owner_factory = _Factory(source)
    unused_factory = _Factory(error=AssertionError("attach must not rebuild source"))

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        owner = await channel.wrap_recoverable(
            owner_factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        owner_delivery = aiter(owner)
        first = await anext(owner_delivery)
        attached = await channel.wrap_recoverable(
            unused_factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        release.set()

        with pytest.raises(StopAsyncIteration):
            await anext(owner_delivery)
        assert await _data(attached) == ["one"]

    assert first.envelope.message_id == "stable-message-1"
    assert owner_factory.checkpoints == [None]
    assert unused_factory.checkpoints == []
    assert source.close_calls == 1


async def test_recoverable_factory_failure_settles_run_before_returning(
    messaging_backend: MessagingBackend,
) -> None:
    cause = RuntimeError("cannot reopen source")
    failed_factory = _Factory(error=cause)
    unused_factory = _Factory(error=AssertionError("failed run must be replay-only"))

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(RuntimeError, match="cannot reopen source"):
            await channel.wrap_recoverable(
                failed_factory,
                stream="conversation-1",
                run="run-1",
                after=0,
            )

        replay = await channel.wrap_recoverable(
            unused_factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        with pytest.raises(RunProducerFailed) as captured:
            await anext(aiter(replay))

    assert captured.value.cause is not None
    assert "cannot reopen source" in str(captured.value.cause)
    assert failed_factory.checkpoints == [None]
    assert unused_factory.checkpoints == []


async def test_ownership_loss_dominates_a_simultaneous_source_open_failure() -> None:
    barrier = asyncio.Barrier(2)
    backend = _ConcurrentRenewalFailureBackend(barrier)

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())

        with pytest.raises(
            BackendOwnershipLost,
            match="owner lease was lost",
        ) as captured:
            await channel.wrap_recoverable(
                _ConcurrentOpenFailureFactory(barrier),
                stream="conversation-1",
                run="run-1",
                after=0,
            )

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "source reconstruction failed"


def test_recoverable_checkpoint_must_match_the_stable_message_id() -> None:
    with pytest.raises(
        ValueError,
        match="checkpoint.last_message_id must match message_id",
    ):
        RecoverableMessage(
            message_id="stable-message-1",
            data="not-committed",
            checkpoint=RecoveryCheckpoint(
                position=b"1",
                last_message_id="different-message",
            ),
        )


async def test_recoverable_cancel_callback_returns_checkpointed_tail(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    first = RecoverableMessage(
        message_id="stable-message-1",
        data="one",
        checkpoint=RecoveryCheckpoint(
            position=b"1",
            last_message_id="stable-message-1",
        ),
    )
    tail = RecoverableMessage(
        message_id="stable-message-2",
        data="cancelled-tail",
        checkpoint=RecoveryCheckpoint(
            position=b"2",
            last_message_id="stable-message-2",
        ),
    )
    source = _Source(first, release=release)
    received: list[CancelContext] = []

    async def cancel_run(
        context: CancelContext,
    ) -> tuple[RecoverableMessage[str], ...]:
        received.append(context)
        release.set()
        return (tail,)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap_recoverable(
            _Factory(source),
            stream="conversation-1",
            run="run-1",
            after=0,
            cancel=cancel_run,
        )
        await asyncio.wait_for(source.started.wait(), timeout=1)

        assert await channel.cancel(stream="conversation-1", run="run-1") is True
        replay = [message async for message in subscription]

    assert [message.data for message in replay] == ["one", "cancelled-tail"]
    assert [message.envelope.message_id for message in replay] == [
        "stable-message-1",
        "stable-message-2",
    ]
    assert received == [
        CancelContext(channel="events", stream="conversation-1", run="run-1")
    ]
