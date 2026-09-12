"""AG-UI source preparation, event ordering, and cleanup through public adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from typing import cast

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import (
    CancelContext,
    CancellableMessageSource,
    DeferredMessageSource,
    MessageSource,
    MessageSourceBinding,
    Messaging,
    create_agui_run_source,
    map_source,
)


class _Events:
    messaging_codec_profile = "agui.event"
    messaging_source_type = BaseEvent
    messaging_replay_type = BaseEvent
    messaging_cancel_waits_for_first_item = True

    def __init__(self) -> None:
        self.messaging_identity = RunIdentity(
            namespace="user-a", thread_id="conversation", run_id="run"
        )
        self.calls: list[str] = []
        self.prepare_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.close_count = 0
        self.cancel_count = 0
        self.empty = False
        self.pull_error: BaseException | None = None

    async def messaging_owner_preflight(self) -> None:
        self.calls.append("prepare")
        if self.prepare_error is not None:
            raise self.prepare_error

    def __aiter__(self) -> AsyncGenerator[BaseEvent]:
        async def events() -> AsyncGenerator[BaseEvent]:
            self.calls.append("pull")
            if self.pull_error is not None:
                raise self.pull_error
            if not self.empty:
                yield RunStartedEvent(thread_id="conversation", run_id="run")
                yield RunFinishedEvent(thread_id="conversation", run_id="run")

        return events()

    @property
    def messaging_cancel_callback(self) -> Callable[[], Awaitable[list[BaseEvent]]]:
        return self.cancel

    async def cancel(self) -> list[BaseEvent]:
        self.cancel_count += 1
        return [RunErrorEvent(message="cancelled", code="cancelled")]

    async def aclose(self) -> None:
        self.close_count += 1
        self.close_started.set()
        await self.close_release.wait()
        self.calls.append("close")
        if self.close_error is not None:
            error, self.close_error = self.close_error, None
            raise error


def _cancel_callback(
    source: MessageSource[BaseEvent],
) -> Callable[[CancelContext], Awaitable[Iterable[BaseEvent] | None]]:
    callback = cast(
        CancellableMessageSource[BaseEvent], source
    ).messaging_cancel_callback
    assert callback is not None
    return cast(
        Callable[[CancelContext], Awaitable[Iterable[BaseEvent] | None]], callback
    )


async def test_owner_preparation_precedes_ready_and_first_pull() -> None:
    upstream = _Events()
    source = create_agui_run_source(upstream)
    assert upstream.calls == []

    async def ready() -> None:
        assert upstream.calls == ["prepare"]
        upstream.calls.append("ready")

    async with Messaging() as messaging:
        subscription = await messaging.channel(name="events").wrap(
            source, on_source_ready=ready
        )
        events = [message.data async for message in subscription]
    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert upstream.calls == ["prepare", "ready", "pull", "close"]


async def test_unused_close_never_prepares_or_consumes_source() -> None:
    upstream = _Events()
    source = create_agui_run_source(upstream)
    await source.aclose()
    await source.aclose()
    assert upstream.calls == ["close"]
    assert upstream.close_count == 1


async def test_cancel_waits_for_first_transform_and_uses_same_transform_for_tail() -> (
    None
):
    upstream = _Events()
    transforming, release, cancelling = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    transformed: list[str] = []

    async def transform(event: BaseEvent) -> BaseEvent:
        if isinstance(event, RunStartedEvent):
            transforming.set()
            await release.wait()
        transformed.append(event.type.value)
        return event.model_copy(update={"label": "visible"})

    source = create_agui_run_source(upstream, transform_event=transform)
    first = asyncio.ensure_future(anext(aiter(source)))

    async def cancel() -> Iterable[BaseEvent] | None:
        cancelling.set()
        return await _cancel_callback(source)(
            CancelContext(channel="events", identity=upstream.messaging_identity)
        )

    await transforming.wait()
    cancelling_task = asyncio.create_task(cancel())
    try:
        await cancelling.wait()
        assert upstream.cancel_count == 0
        assert transformed == []
        release.set()
        assert (await first).type.value == "RUN_STARTED"
        tail = await cancelling_task
        assert tail is not None
        assert [event.type.value for event in tail] == ["RUN_ERROR"]
        assert transformed == ["RUN_STARTED", "RUN_ERROR"]
        assert all(event.model_extra == {"label": "visible"} for event in tail)
    finally:
        release.set()
        await asyncio.gather(first, cancelling_task, return_exceptions=True)
        await source.aclose()


@pytest.mark.parametrize("outcome", ["empty", "failure", "closed"])
async def test_first_pull_terminal_paths_release_cancel_waiters(outcome: str) -> None:
    upstream = _Events()
    upstream.empty = outcome == "empty"
    failure = ValueError("upstream pull failed")
    upstream.pull_error = failure if outcome == "failure" else None
    source = create_agui_run_source(upstream)
    cancel = asyncio.ensure_future(
        _cancel_callback(source)(
            CancelContext(channel="events", identity=upstream.messaging_identity)
        )
    )
    try:
        if outcome == "closed":
            await source.aclose()
        else:
            with pytest.raises(
                StopAsyncIteration if outcome == "empty" else ValueError
            ):
                await anext(aiter(source))
        await cancel
        assert upstream.cancel_count == (0 if outcome == "closed" else 1)
    finally:
        await source.aclose()
        await asyncio.gather(cancel, return_exceptions=True)


@pytest.mark.parametrize("adapter", ["agui", "mapped", "deferred"])
async def test_explicit_close_can_finish_upstream_after_a_failed_wait(
    adapter: str,
) -> None:
    upstream = _Events()
    failure = TimeoutError("cleanup has not finished")
    cause = OSError("provider still settling")
    failure.__cause__ = cause
    upstream.close_error = failure
    source: MessageSource[BaseEvent]
    if adapter == "agui":
        source = create_agui_run_source(upstream)
    elif adapter == "mapped":
        source = map_source(upstream, lambda event: event)
    else:

        async def open_source() -> MessageSourceBinding[BaseEvent]:
            return MessageSourceBinding(upstream)

        source = DeferredMessageSource(open_source, cancellable=True)
        await anext(aiter(source))
    with pytest.raises(TimeoutError) as caught:
        await source.aclose()
    assert caught.value is failure
    assert caught.value.__cause__ is cause
    assert upstream.close_count == 1
    await source.aclose()
    await source.aclose()
    assert upstream.close_count == 2


async def test_close_waits_for_active_transform_cleanup() -> None:
    upstream = _Events()
    transforming, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def transform(event: BaseEvent) -> BaseEvent:
        transforming.set()
        try:
            await asyncio.Event().wait()
        finally:
            settling.set()
            await release.wait()
        return event

    source = create_agui_run_source(upstream, transform_event=transform)
    pull = asyncio.ensure_future(anext(aiter(source)))
    await transforming.wait()
    close = asyncio.create_task(source.aclose())
    try:
        await settling.wait()
        assert not close.done()
        assert upstream.close_count == 0
        release.set()
        await close
        with pytest.raises(asyncio.CancelledError):
            await pull
        assert upstream.close_count == 1
    finally:
        release.set()
        await asyncio.gather(pull, close, return_exceptions=True)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
async def test_double_cancelled_close_retains_independent_cleanup_failure(
    error_type: type[BaseException],
) -> None:
    upstream = _Events()
    failure = error_type("cleanup provider failure")
    cause = LookupError("cleanup original cause")
    failure.__cause__ = cause
    upstream.close_error = failure
    upstream.close_release.clear()
    source = create_agui_run_source(upstream)
    observed: list[BaseException] = []

    async def caller() -> None:
        try:
            await source.aclose()
        except BaseException as error:  # noqa: BLE001 - inspect control in its owner
            observed.append(error)

    close = asyncio.create_task(caller())
    await upstream.close_started.wait()
    close.cancel("first")
    close.cancel("second")
    upstream.close_release.set()
    await close
    assert len(observed) == 1
    expected = asyncio.CancelledError if error_type is OSError else error_type
    assert isinstance(observed[0], expected)
    pending = [observed[0]]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    assert id(failure) in seen and id(cause) in seen
    assert upstream.close_count == 1
    await source.aclose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("messaging_codec_profile", "other"),
        ("messaging_source_type", str),
        ("messaging_replay_type", str),
        ("messaging_identity", "not-an-identity"),
        ("messaging_cancel_waits_for_first_item", False),
        ("messaging_cancel_callback", None),
    ],
)
async def test_invalid_source_is_rejected_before_cleanup_ownership_transfers(
    field: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _Events()
    with monkeypatch.context() as patch:
        if field == "messaging_cancel_callback":
            patch.setattr(_Events, field, value)
        else:
            patch.setattr(upstream, field, value)
        with pytest.raises(TypeError):
            create_agui_run_source(upstream)
    assert upstream.calls == []
    await upstream.aclose()


@pytest.mark.parametrize(
    "error_type", [OSError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
async def test_preparation_and_cleanup_failures_keep_both_original_causes(
    error_type: type[BaseException],
) -> None:
    upstream = _Events()
    preparation = error_type("preparation failed")
    preparation_cause = LookupError("preparation original cause")
    preparation.__cause__ = preparation_cause
    cleanup = OSError("cleanup failed")
    cleanup_cause = LookupError("cleanup original cause")
    cleanup.__cause__ = cleanup_cause
    upstream.prepare_error = preparation
    upstream.close_error = cleanup
    source = create_agui_run_source(upstream)
    async with Messaging() as messaging:
        with pytest.raises(error_type) as caught:
            await messaging.channel(name="events").wrap(source)
    assert caught.value is preparation
    pending = [caught.value]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend(
            item for item in (error.__cause__, error.__context__) if item is not None
        )
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    assert all(
        id(error) in seen for error in (preparation_cause, cleanup, cleanup_cause)
    )
    assert upstream.calls == ["prepare", "close"]
