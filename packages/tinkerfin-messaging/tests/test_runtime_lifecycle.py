"""Messaging, source, producer, and subscription lifecycle contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar, Literal, cast

import pytest

import tinkerfin_messaging
from tinkerfin_messaging import (
    BackendOwnershipLost,
    BackendRunHandle,
    MemoryBackend,
    MessageEnvelope,
    MessageSubscription,
    Messaging,
    MessagingBackend,
    MessagingClosed,
    MessagingNotStarted,
    PreparedRun,
    RecoverableMessage,
    RecoveryCheckpoint,
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
        *items: str,
        release: asyncio.Event | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.items = items
        self.release = release
        self.error = error
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self.items:
                yield item
            if self.release is not None:
                await self.release.wait()
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


class _TrackingIterator:
    def __init__(
        self,
        iterator: AsyncIterator[MessageEnvelope],
        backend: _TrackingBackend,
    ) -> None:
        self._iterator = iterator
        self._backend = backend
        self._closed = False

    def __aiter__(self) -> _TrackingIterator:
        return self

    async def __anext__(self) -> MessageEnvelope:
        return await anext(self._iterator)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.follow_close_calls += 1
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()


class _TrackingBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.follow_close_calls = 0

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncIterator[MessageEnvelope]:
        return _TrackingIterator(
            super().follow(handle, after=after),
            self,
        )


class _LeasedMemoryBackend(MemoryBackend):
    @property
    def lease_renew_interval(self) -> float:
        return 0.01


class _BlockingPrepareBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare(
        self,
        *,
        channel: str,
        stream: str,
        run: str,
        codec: str,
        identity: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun:
        self.entered.set()
        await self.release.wait()
        return await super().prepare(
            channel=channel,
            stream=stream,
            run=run,
            codec=codec,
            identity=identity,
            after=after,
            cancellable=cancellable,
            recoverable=recoverable,
        )


class _BlockingAppendBackend(MemoryBackend):
    def __init__(self, *, blocked_payload: bytes = b"one") -> None:
        super().__init__()
        self.blocked_payload = blocked_payload
        self.append_started = asyncio.Event()
        self.release_append = asyncio.Event()
        self.append_cancelled = asyncio.Event()

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        if payload == self.blocked_payload:
            self.append_started.set()
            try:
                await self.release_append.wait()
            except asyncio.CancelledError:
                self.append_cancelled.set()
                raise
        return await super().append(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )


class _CountingBlockingAppendBackend(_BlockingAppendBackend):
    def __init__(self, *, blocked_payload: bytes = b"one") -> None:
        super().__init__(blocked_payload=blocked_payload)
        self.finish_calls = 0

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: Literal["completed", "cancelled", "failed", "owner_lost"],
        error: BaseException | None = None,
    ) -> None:
        self.finish_calls += 1
        await super().finish(handle, status=status, error=error)


class _FinishFailureBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.finish_started = asyncio.Event()
        self.release_finish = asyncio.Event()
        self.finish_calls = 0

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: Literal["completed", "cancelled", "failed", "owner_lost"],
        error: BaseException | None = None,
    ) -> None:
        del status, error
        self.finish_calls += 1
        self.finish_started.set()
        await self.release_finish.wait()
        raise BackendOwnershipLost(
            f"Producer for run {handle.run!r} lost ownership during finish"
        )


class _DelayedCancelObservationBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_is_durable = asyncio.Event()
        self.release_observer = asyncio.Event()

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        requested = await super().wait_for_cancel(handle)
        if requested:
            self.cancel_is_durable.set()
            await self.release_observer.wait()
        return requested


class _BlockingSettlementBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.settlement_entered = asyncio.Event()
        self.release_settlement = asyncio.Event()

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        self.settlement_entered.set()
        await self.release_settlement.wait()
        return await super().begin_settlement(handle)


class _ClaimThenBlockSettlementBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_is_durable = asyncio.Event()
        self.release_observer = asyncio.Event()
        self.settlement_claimed = asyncio.Event()
        self.release_response = asyncio.Event()

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        requested = await super().wait_for_cancel(handle)
        if requested:
            self.cancel_is_durable.set()
            await self.release_observer.wait()
        return requested

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        result = await super().begin_settlement(handle)
        self.settlement_claimed.set()
        await self.release_response.wait()
        return result


class _RecoverableSource:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
        async def iterate() -> AsyncGenerator[RecoverableMessage[str], None]:
            yield RecoverableMessage(
                message_id="stable-message-1",
                data="one",
                checkpoint=RecoveryCheckpoint(
                    position=b"1",
                    last_message_id="stable-message-1",
                ),
            )

        return iterate()

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _BlockingRecoveryFactory:
    def __init__(self, source: _RecoverableSource | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.source = source or _RecoverableSource()

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        assert checkpoint is None
        self.entered.set()
        await self.release.wait()
        return self.source


class _CancellationResistantRecoveryFactory:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.source = _RecoverableSource()

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        assert checkpoint is None
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return self.source
        raise AssertionError("unreachable")


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


def test_messaging_backend_is_read_only_after_construction() -> None:
    backend = MemoryBackend()
    messaging = Messaging(backend=backend)

    assert messaging.backend is backend
    with pytest.raises(AttributeError):
        setattr(messaging, "backend", MemoryBackend())


async def test_messaging_is_single_use_and_requires_an_open_lifecycle() -> None:
    messaging = Messaging()

    with pytest.raises(MessagingNotStarted):
        messaging.channel(name="events", codec=_TextCodec())

    async with messaging:
        messaging.channel(name="events", codec=_TextCodec())

    with pytest.raises(MessagingClosed):
        messaging.channel(name="events", codec=_TextCodec())
    with pytest.raises(MessagingClosed):
        await messaging.__aenter__()


async def test_backend_subscription_closes_after_normal_completion() -> None:
    backend = _TrackingBackend()
    source = _Source("one", "two")

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        assert await _data(subscription) == ["one", "two"]

    assert backend.follow_close_calls == 1


async def test_backend_subscription_closes_on_early_detach() -> None:
    backend = _TrackingBackend()
    release = asyncio.Event()
    source = _Source("one", release=release)

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "one"

        await subscription.aclose()
        assert backend.follow_close_calls == 1
        assert not source.closed.is_set()
        release.set()
        await asyncio.wait_for(source.closed.wait(), timeout=1)


async def test_never_iterated_subscription_closes_without_claiming_delivery() -> None:
    backend = _TrackingBackend()
    source = _Source("one")

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )

        await subscription.aclose()

    assert backend.follow_close_calls == 0
    assert source.close_calls == 1


async def test_backend_subscription_closes_on_producer_failure() -> None:
    backend = _TrackingBackend()
    cause = RuntimeError("source failed")
    source = _Source("one", error=cause)

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        delivery = aiter(subscription)
        assert (await anext(delivery)).data == "one"
        with pytest.raises(RunProducerFailed):
            await anext(delivery)

    assert backend.follow_close_calls == 1


async def test_wrap_cancellation_closes_an_unclaimed_source() -> None:
    backend = _BlockingPrepareBackend()
    source = _Source("unused")

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        wrapping = asyncio.create_task(
            channel.wrap(
                source,
                stream="conversation-1",
                run="run-1",
                after=0,
            )
        )
        await asyncio.wait_for(backend.entered.wait(), timeout=1)
        wrapping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapping

    assert source.close_calls == 1
    assert not source.started.is_set()


async def test_shutdown_waits_for_inflight_prepare_and_rejects_late_producer() -> None:
    backend = _BlockingPrepareBackend()
    release = asyncio.Event()
    source = _Source("one", release=release)
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    wrapping = asyncio.create_task(
        channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
    )
    await asyncio.wait_for(backend.entered.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    backend.release.set()
    wrap_result, _ = await asyncio.gather(
        wrapping,
        closing,
        return_exceptions=True,
    )
    release.set()
    if isinstance(wrap_result, MessageSubscription):
        await _data(wrap_result)

    assert shutdown_waited
    assert isinstance(wrap_result, MessagingClosed)
    assert source.close_calls == 1


async def test_shutdown_waits_for_recoverable_open_and_rejects_late_producer() -> None:
    factory = _BlockingRecoveryFactory()
    messaging = Messaging()
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    wrapping = asyncio.create_task(
        channel.wrap_recoverable(
            factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    factory.release.set()
    wrap_result, _ = await asyncio.gather(
        wrapping,
        closing,
        return_exceptions=True,
    )
    if isinstance(wrap_result, MessageSubscription):
        await _data(wrap_result)

    assert shutdown_waited
    assert isinstance(wrap_result, MessagingClosed)
    assert factory.source.close_calls == 1


async def test_recoverable_preflight_settles_owner_when_source_close_fails() -> None:
    backend = MemoryBackend()
    close_error = RuntimeError("cannot close rebuilt source")
    source = _RecoverableSource(close_error=close_error)
    factory = _BlockingRecoveryFactory(source)
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    wrapping = asyncio.create_task(
        channel.wrap_recoverable(
            factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
    )
    await asyncio.wait_for(factory.entered.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    factory.release.set()
    wrap_result, _ = await asyncio.gather(
        wrapping,
        closing,
        return_exceptions=True,
    )

    async with Messaging(backend=backend) as observer:
        resumed = await observer.channel(
            name="events",
            codec=_TextCodec(),
        ).wrap(
            _Source("next-run"),
            stream="conversation-1",
            run="run-2",
            after=None,
        )
        assert await _data(resumed) == ["next-run"]

    assert wrap_result is close_error
    assert source.close_calls == 1


async def test_cancelled_recoverable_open_closes_a_late_returned_source() -> None:
    factory = _CancellationResistantRecoveryFactory()

    async with Messaging(backend=_LeasedMemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        wrapping = asyncio.create_task(
            channel.wrap_recoverable(
                factory,
                stream="conversation-1",
                run="run-1",
                after=0,
            )
        )
        await asyncio.wait_for(factory.entered.wait(), timeout=1)
        wrapping.cancel()

        with pytest.raises(asyncio.CancelledError):
            await wrapping

    assert factory.source.close_calls == 1


async def test_messaging_shutdown_closes_active_source_and_owned_tasks(
    messaging_backend: MessagingBackend,
) -> None:
    release = asyncio.Event()
    source = _Source("one", release=release)
    messaging = Messaging(backend=messaging_backend, settlement_timeout=2)

    async def cancel() -> None:
        release.set()

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    await asyncio.wait_for(source.started.wait(), timeout=1)
    await messaging.__aexit__(None, None, None)

    await asyncio.sleep(0)
    live_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    assert source.close_calls == 1
    assert not any(name.startswith("tinkerfin-messaging-") for name in live_names)


async def test_immediate_shutdown_after_wrap_closes_source_and_settles_run() -> None:
    backend = MemoryBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
    )

    await messaging.__aexit__(None, None, None)

    assert source.close_calls == 1
    status = await asyncio.wait_for(
        backend.wait_finished(
            BackendRunHandle(
                channel="events",
                stream="conversation-1",
                run="run-1",
                owner_token=None,
                fence=None,
            )
        ),
        timeout=1,
    )
    assert status == "failed"


async def test_shutdown_waits_for_an_accepted_cancel_callback_tail(
    messaging_backend: MessagingBackend,
) -> None:
    source_release = asyncio.Event()
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=messaging_backend)

    async def cancel() -> tuple[str, ...]:
        callback_started.set()
        await callback_release.wait()
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(
        channel.cancel(stream="conversation-1", run="run-1")
    )
    await asyncio.wait_for(callback_started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    callback_release.set()
    cancel_result = await asyncio.wait_for(cancelling, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    replay = [first.data, *[message.data async for message in delivery]]

    assert shutdown_waited
    assert cancel_result is True
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1


async def test_shutdown_honors_durable_cancel_before_watcher_returns() -> None:
    backend = _DelayedCancelObservationBackend()
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    callback_calls = 0
    messaging = Messaging(backend=backend)

    async def cancel() -> tuple[str, ...]:
        nonlocal callback_calls
        callback_calls += 1
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(
        channel.cancel(stream="conversation-1", run="run-1")
    )
    await asyncio.wait_for(backend.cancel_is_durable.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    status = await asyncio.wait_for(
        backend.wait_finished(
            BackendRunHandle(
                channel="events",
                stream="conversation-1",
                run="run-1",
                owner_token=None,
                fence=None,
            )
        ),
        timeout=1,
    )
    backend.release_observer.set()
    cancel_result = await asyncio.wait_for(cancelling, timeout=1)
    await asyncio.wait_for(closing, timeout=1)
    replay = [first.data, *[message.data async for message in delivery]]

    assert status == "cancelled"
    assert cancel_result is True
    assert callback_calls == 1
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1


async def test_cancel_callback_starts_only_after_settlement_is_claimed() -> None:
    backend = _BlockingSettlementBackend()
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    callback_calls = 0

    async def cancel() -> tuple[str, ...]:
        nonlocal callback_calls
        callback_calls += 1
        source_release.set()
        return ("cancelled-tail",)

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
            cancel=cancel,
        )
        delivery = aiter(subscription)
        first = await anext(delivery)
        cancelling = asyncio.create_task(
            channel.cancel(stream="conversation-1", run="run-1")
        )
        await asyncio.wait_for(backend.settlement_entered.wait(), timeout=1)
        calls_before_claim = callback_calls
        backend.release_settlement.set()
        cancel_result = await asyncio.wait_for(cancelling, timeout=1)
        replay = [first.data, *[message.data async for message in delivery]]

    assert calls_before_claim == 0
    assert cancel_result is True
    assert callback_calls == 1
    assert replay == ["one", "cancelled-tail"]


async def test_cancelled_shutdown_finishes_a_claimed_settlement() -> None:
    backend = _ClaimThenBlockSettlementBackend()
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    callback_calls = 0
    messaging = Messaging(backend=backend)

    async def cancel() -> tuple[str, ...]:
        nonlocal callback_calls
        callback_calls += 1
        source_release.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(
        channel.cancel(stream="conversation-1", run="run-1")
    )
    await asyncio.wait_for(backend.cancel_is_durable.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.wait_for(backend.settlement_claimed.wait(), timeout=1)
    closing.cancel()
    done, _ = await asyncio.wait({closing}, timeout=0.05)
    closed_before_release = closing in done
    backend.release_response.set()
    backend.release_observer.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    cancel_result = await asyncio.gather(cancelling, return_exceptions=True)
    replay_failure: RunProducerFailed | None = None
    replay = [first.data]
    try:
        replay.extend([message.data async for message in delivery])
    except RunProducerFailed as failure:
        replay_failure = failure

    live_names = {task.get_name() for task in asyncio.all_tasks() if not task.done()}
    assert not closed_before_release
    assert cancel_result == [True]
    assert replay_failure is None
    assert callback_calls == 1
    assert replay == ["one", "cancelled-tail"]
    assert source.close_calls == 1
    assert not any(name.startswith("tinkerfin-messaging-") for name in live_names)


async def test_messaging_shutdown_cancels_an_inflight_backend_append() -> None:
    backend = _BlockingAppendBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
    )
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    done, _ = await asyncio.wait({closing}, timeout=0.1)
    closed_without_backend_release = closing in done
    if not closed_without_backend_release:
        backend.release_append.set()
    await asyncio.wait_for(closing, timeout=1)

    assert closed_without_backend_release
    assert backend.append_cancelled.is_set()
    assert source.close_calls == 1


async def test_messaging_shutdown_waits_for_cancel_tail_settlement() -> None:
    backend = _BlockingAppendBackend(blocked_payload=b"cancelled-tail")
    release_source = asyncio.Event()
    source = _Source("one", release=release_source)
    messaging = Messaging(backend=backend)

    async def cancel() -> tuple[str, ...]:
        release_source.set()
        return ("cancelled-tail",)

    await messaging.__aenter__()
    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    assert (await anext(delivery)).data == "one"
    cancelling = asyncio.create_task(
        channel.cancel(stream="conversation-1", run="run-1")
    )
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)

    closing = asyncio.create_task(messaging.__aexit__(None, None, None))
    await asyncio.sleep(0)
    shutdown_waited = not closing.done()
    backend.release_append.set()
    if shutdown_waited:
        assert await asyncio.wait_for(cancelling, timeout=1) is True
    else:
        cancelling.cancel()
        await asyncio.gather(cancelling, return_exceptions=True)
        orphan_committers = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "tinkerfin-messaging-committer:run-1"
        ]
        for task in orphan_committers:
            task.cancel()
        await asyncio.gather(*orphan_committers, return_exceptions=True)
    await asyncio.wait_for(closing, timeout=1)

    assert shutdown_waited
    assert source.close_calls == 1


@pytest.mark.parametrize(
    ("value", "error_type"),
    (
        pytest.param(True, TypeError, id="boolean"),
        pytest.param("1", TypeError, id="string"),
        pytest.param(-0.01, ValueError, id="negative"),
        pytest.param(float("inf"), ValueError, id="infinity"),
        pytest.param(float("nan"), ValueError, id="nan"),
    ),
)
def test_messaging_rejects_invalid_settlement_timeout(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        Messaging(settlement_timeout=cast(float | None, value))


@pytest.mark.parametrize("value", (None, 0, 0.01))
def test_messaging_accepts_non_negative_settlement_timeout(
    value: float | None,
) -> None:
    Messaging(settlement_timeout=value)


async def test_finite_close_budget_keeps_cancel_tail_settlement_owned() -> None:
    backend = _CountingBlockingAppendBackend(blocked_payload=b"cancelled-tail")
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=backend, settlement_timeout=0.01)
    await messaging.__aenter__()

    async def cancel() -> tuple[str, ...]:
        source_release.set()
        return ("cancelled-tail",)

    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(
        channel.cancel(stream="conversation-1", run="run-1")
    )
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)
    timeout_type = getattr(
        tinkerfin_messaging,
        "MessagingSettlementTimeout",
        None,
    )
    assert timeout_type is not None

    try:
        with pytest.raises(timeout_type) as captured:
            await messaging.aclose()

        assert captured.value.timeout == 0.01
        assert not backend.append_cancelled.is_set()
        assert not cancelling.done()
        with pytest.raises(MessagingClosed):
            messaging.channel(name="closed", codec=_TextCodec())

        backend.release_append.set()
        assert await asyncio.wait_for(cancelling, timeout=1) is True
        await messaging.aclose()
        replay = [first.data, *[message.data async for message in delivery]]

        assert replay == ["one", "cancelled-tail"]
        assert source.close_calls == 1
        assert backend.finish_calls == 1
    finally:
        backend.release_append.set()
        await asyncio.gather(cancelling, return_exceptions=True)
        await asyncio.gather(messaging.aclose(), return_exceptions=True)


async def test_default_close_budget_waits_for_complete_settlement() -> None:
    backend = _CountingBlockingAppendBackend(blocked_payload=b"cancelled-tail")
    source_release = asyncio.Event()
    source = _Source("one", release=source_release)
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()

    async def cancel() -> tuple[str, ...]:
        source_release.set()
        return ("cancelled-tail",)

    channel = messaging.channel(name="events", codec=_TextCodec())
    subscription = await channel.wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
        cancel=cancel,
    )
    delivery = aiter(subscription)
    first = await anext(delivery)
    cancelling = asyncio.create_task(
        channel.cancel(stream="conversation-1", run="run-1")
    )
    await asyncio.wait_for(backend.append_started.wait(), timeout=1)
    closing = asyncio.create_task(messaging.aclose())

    try:
        done, _ = await asyncio.wait({closing}, timeout=0.05)
        assert closing not in done
        assert not backend.append_cancelled.is_set()

        backend.release_append.set()
        assert await asyncio.wait_for(cancelling, timeout=1) is True
        await asyncio.wait_for(closing, timeout=1)
        replay = [first.data, *[message.data async for message in delivery]]

        assert replay == ["one", "cancelled-tail"]
        assert source.close_calls == 1
        assert backend.finish_calls == 1
    finally:
        backend.release_append.set()
        await asyncio.gather(cancelling, closing, return_exceptions=True)


async def test_close_only_failure_propagates_and_is_idempotent() -> None:
    backend = _FinishFailureBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    await messaging.channel(name="events", codec=_TextCodec()).wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
    )
    await asyncio.wait_for(source.started.wait(), timeout=1)
    backend.release_finish.set()

    with pytest.raises(BackendOwnershipLost) as first:
        await messaging.aclose()
    with pytest.raises(BackendOwnershipLost) as second:
        await messaging.aclose()

    assert second.value is first.value
    assert source.close_calls == 1
    assert backend.finish_calls == 1


@pytest.mark.parametrize(
    ("body_error", "error_type"),
    (
        pytest.param(ValueError("body failed"), ValueError, id="business-error"),
        pytest.param(
            asyncio.CancelledError("body cancelled"),
            asyncio.CancelledError,
            id="body-cancellation",
        ),
    ),
)
async def test_context_body_failure_outranks_close_failure(
    body_error: BaseException,
    error_type: type[BaseException],
) -> None:
    backend = _FinishFailureBackend()
    backend.release_finish.set()
    source = _Source("one", release=asyncio.Event())

    with pytest.raises(error_type) as captured:
        async with Messaging(backend=backend) as messaging:
            await messaging.channel(name="events", codec=_TextCodec()).wrap(
                source,
                stream="conversation-1",
                run="run-1",
                after=0,
            )
            await asyncio.wait_for(source.started.wait(), timeout=1)
            raise body_error

    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert captured.value is body_error
    assert "BackendOwnershipLost" in notes
    assert source.close_calls == 1
    assert backend.finish_calls == 1


async def test_caller_cancellation_outranks_late_close_failure() -> None:
    backend = _FinishFailureBackend()
    source = _Source("one", release=asyncio.Event())
    messaging = Messaging(backend=backend)
    await messaging.__aenter__()
    await messaging.channel(name="events", codec=_TextCodec()).wrap(
        source,
        stream="conversation-1",
        run="run-1",
        after=0,
    )
    await asyncio.wait_for(source.started.wait(), timeout=1)
    closing = asyncio.create_task(messaging.aclose())
    await asyncio.wait_for(backend.finish_started.wait(), timeout=1)

    closing.cancel("caller cancelled close")
    done, _ = await asyncio.wait({closing}, timeout=0.05)
    settled_before_finish = closing in done
    backend.release_finish.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await closing

    notes = "\n".join(getattr(captured.value, "__notes__", ()))
    assert not settled_before_finish
    assert "BackendOwnershipLost" in notes
    assert source.close_calls == 1
    assert backend.finish_calls == 1
