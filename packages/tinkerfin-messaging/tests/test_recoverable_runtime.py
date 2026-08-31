"""Recoverable factory, stable ID, and lifecycle contracts across backends."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import ClassVar

import pytest
from backend_harness import MessagingBackendHarness

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    BackendOwnershipLost,
    CancelContext,
    MemoryBackend,
    MessageSubscription,
    Messaging,
    MessagingBackendSettings,
    MessagingTransition,
    MessagingTransitionResult,
    RecoverableMessage,
    RecoveryCheckpoint,
    RunProducerFailed,
)


def _identity() -> RunIdentity:
    return RunIdentity(threadId="conversation-1", runId="run-1")


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
    def messaging_settings(self) -> MessagingBackendSettings:
        return replace(
            super().messaging_settings,
            producer_renew_interval_seconds=0.001,
            producer_lease_seconds=0.003,
        )

    async def commit_messaging_transition(
        self,
        transition: MessagingTransition,
    ) -> MessagingTransitionResult:
        if transition.kind == "renew_producer_ownership":
            await self._barrier.wait()
            raise BackendOwnershipLost("recoverable source owner lease was lost")
        return await super().commit_messaging_transition(transition)


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def test_recoverable_owner_uses_stable_id_and_attach_does_not_open_factory(
    messaging_backend: MessagingBackendHarness,
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
    owner_starting = 0
    owner_not_started = 0
    attachment_starting = 0
    attachment_not_started = 0

    async def source_starting() -> None:
        nonlocal owner_starting
        assert owner_factory.checkpoints == []
        owner_starting += 1

    async def delivery_not_started() -> None:
        nonlocal owner_not_started
        owner_not_started += 1

    async def attached_source_starting() -> None:
        nonlocal attachment_starting
        attachment_starting += 1

    async def attached_delivery_not_started() -> None:
        nonlocal attachment_not_started
        attachment_not_started += 1

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        owner = await channel.wrap_recoverable(
            owner_factory,
            identity=_identity(),
            after=0,
            on_source_starting=source_starting,
            on_delivery_not_started=delivery_not_started,
        )
        owner_delivery = aiter(owner)
        first = await anext(owner_delivery)
        attached = await channel.wrap_recoverable(
            unused_factory,
            identity=_identity(),
            after=0,
            on_source_starting=attached_source_starting,
            on_delivery_not_started=attached_delivery_not_started,
        )
        release.set()

        with pytest.raises(StopAsyncIteration):
            await anext(owner_delivery)
        assert await _data(attached) == ["one"]

    assert first.envelope.message_id == "stable-message-1"
    assert owner_factory.checkpoints == [None]
    assert unused_factory.checkpoints == []
    assert source.close_calls == 1
    assert owner_starting == 1
    assert owner_not_started == 0
    assert attachment_starting == 0
    assert attachment_not_started == 0


async def test_recoverable_factory_failure_settles_run_before_returning(
    messaging_backend: MessagingBackendHarness,
) -> None:
    cause = RuntimeError("cannot reopen source")
    failed_factory = _Factory(error=cause)
    unused_factory = _Factory(error=AssertionError("failed run must be replay-only"))
    not_started_statuses: list[str] = []

    async def delivery_not_started() -> None:
        status = await messaging_backend.get_run_status(
            channel="events",
            identity=_identity(),
        )
        not_started_statuses.append(status)

    async with Messaging(backend=messaging_backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(RuntimeError, match="cannot reopen source"):
            await channel.wrap_recoverable(
                failed_factory,
                identity=_identity(),
                after=0,
                on_delivery_not_started=delivery_not_started,
            )

        replay = await channel.wrap_recoverable(
            unused_factory,
            identity=_identity(),
            after=0,
        )
        with pytest.raises(RunProducerFailed) as captured:
            await anext(aiter(replay))

    assert captured.value.cause is not None
    assert "cannot reopen source" in str(captured.value.cause)
    assert failed_factory.checkpoints == [None]
    assert unused_factory.checkpoints == []
    assert not_started_statuses == ["failed"]


async def test_ownership_loss_dominates_a_simultaneous_source_open_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    barrier = asyncio.Barrier(2)
    backend = _ConcurrentRenewalFailureBackend(barrier)

    with caplog.at_level(logging.ERROR, logger="tinkerfin.messaging"):
        async with Messaging(backend=backend) as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())

            with pytest.raises(
                BackendOwnershipLost,
                match="owner lease was lost",
            ) as captured:
                await channel.wrap_recoverable(
                    _ConcurrentOpenFailureFactory(barrier),
                    identity=_identity(),
                    after=0,
                )

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "source reconstruction failed"
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Messaging producer lease renewal failed"
    )
    fields = vars(record)
    assert fields["tinkerfin_renewal_phase"] == "source_open"
    assert fields["tinkerfin_renewal_outcome"] == "backend_exception"
    assert fields["tinkerfin_error_type"] == "BackendOwnershipLost"


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
    messaging_backend: MessagingBackendHarness,
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
            identity=_identity(),
            after=0,
            cancel=cancel_run,
        )
        await asyncio.wait_for(source.started.wait(), timeout=1)

        assert await channel.cancel(identity=_identity()) is True
        replay = [message async for message in subscription]

    assert [message.data for message in replay] == ["one", "cancelled-tail"]
    assert [message.envelope.message_id for message in replay] == [
        "stable-message-1",
        "stable-message-2",
    ]
    assert received == [CancelContext(channel="events", identity=_identity())]
