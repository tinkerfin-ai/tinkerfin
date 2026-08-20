from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunStartedEvent
from langchain_core.messages import AIMessageChunk
from pydantic import ValidationError

from tinkerfin import AgUiEventStream, Identity, TinkerFin


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> Identity:
    return Identity(threadId=thread_id, runId=run_id)


def test_agui_rejects_noncanonical_identity_at_construction() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    with pytest.raises(ValidationError):
        TinkerFin().run(parts, identity=_identity(thread_id=" "))
    with pytest.raises(ValidationError):
        TinkerFin().run(parts, identity=_identity(run_id=""))
    with pytest.raises(ValueError, match="requires an Identity"):
        TinkerFin().run(parts).astream_agui()


def test_agui_rejects_infinite_total_timeout() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    with pytest.raises(ValueError, match="finite and non-negative"):
        TinkerFin().run(parts, identity=_identity()).astream_agui(
            timeout=float("inf"),
        )


@pytest.mark.asyncio
async def test_agui_stream_publishes_its_idempotent_cancel_callback() -> None:
    async def parts() -> AsyncIterator[object]:
        await asyncio.Event().wait()
        if False:  # pragma: no cover - supplies the async iterator shape
            yield None

    identity = _identity()
    stream = TinkerFin().run(parts, identity=identity).astream_agui()

    assert stream.messaging_identity is identity
    assert stream.messaging_cancel_callback == stream.abort
    assert (await anext(stream)).type.value == "RUN_STARTED"
    tail = await stream.messaging_cancel_callback()

    assert [event.type.value for event in tail] == ["RUN_ERROR"]
    assert await stream.messaging_cancel_callback() == []


@pytest.mark.asyncio
async def test_low_level_agui_stream_keeps_its_immutable_identity() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - supplies the async iterator shape
            yield None

    identity = _identity()
    stream = TinkerFin().run(parts, identity=identity).astream_agui()

    started = await anext(stream)

    assert isinstance(started, RunStartedEvent)
    assert stream.messaging_identity is identity
    assert started.input is None
    await stream.aclose()


@pytest.mark.asyncio
async def test_initialization_failure_uses_the_standard_complete_lifecycle() -> None:
    identity = _identity()

    stream = AgUiEventStream.from_initialization_error(
        RuntimeError("cannot initialize runtime"),
        identity=identity,
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    started = events[0]
    failed = events[1]
    assert isinstance(started, RunStartedEvent)
    assert started.input is None
    assert isinstance(failed, RunErrorEvent)
    assert failed.code == "runtime_initialization_error"
    assert failed.raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }


@pytest.mark.asyncio
async def test_initialization_failure_cancel_tail_keeps_release_marker() -> None:
    stream = AgUiEventStream.from_initialization_error(
        RuntimeError("cannot initialize runtime"),
        identity=_identity(),
    )

    started = await anext(stream)
    tail = await stream.messaging_cancel_callback()

    assert isinstance(started, RunStartedEvent)
    assert started.raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }
    assert [event.type.value for event in tail] == ["RUN_ERROR"]
    assert tail[0].raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }


@pytest.mark.asyncio
async def test_agui_observes_converted_events_before_delivery() -> None:
    upstream_closed = asyncio.Event()
    order: list[tuple[str, str]] = []

    async def parts() -> AsyncIterator[object]:
        try:
            yield {
                "type": "values",
                "ns": (),
                "data": {"answer": 42},
                "interrupts": (),
            }
        finally:
            upstream_closed.set()

    async def on_event(event: BaseEvent) -> None:
        order.append(("observed", event.type.value))

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    events: list[BaseEvent] = []
    async for event in stream:
        events.append(event)
        order.append(("delivered", event.type.value))

    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].input is None
    assert order == [
        item
        for event in events
        for item in (
            ("observed", event.type.value),
            ("delivered", event.type.value),
        )
    ]
    assert upstream_closed.is_set()


@pytest.mark.asyncio
async def test_agui_abort_rejects_reentry_from_its_event_observer() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiEventStream | None = None

    async def on_event(_: BaseEvent) -> None:
        assert stream is not None
        await stream.abort()

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )

    with pytest.raises(RuntimeError, match="from its on_event callback"):
        await anext(stream)


@pytest.mark.asyncio
async def test_agui_abort_rejects_child_task_reentry_from_event_observer() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiEventStream | None = None
    abort_task: asyncio.Task[list[BaseEvent]] | None = None

    async def on_event(_: BaseEvent) -> None:
        nonlocal abort_task
        if abort_task is None:
            assert stream is not None
            abort_task = asyncio.create_task(stream.abort())
            await asyncio.sleep(0)

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    consumer = asyncio.create_task(anext(stream))
    try:
        consumer_outcome = (await asyncio.gather(consumer, return_exceptions=True))[0]
        assert abort_task is not None
        abort_outcome = (await asyncio.gather(abort_task, return_exceptions=True))[0]

        assert isinstance(consumer_outcome, BaseEvent)
        assert isinstance(abort_outcome, RuntimeError)
        assert "from its on_event callback" in str(abort_outcome)
    finally:
        await asyncio.gather(consumer, return_exceptions=True)
        if abort_task is not None:
            await asyncio.gather(abort_task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_agui_abort_allows_external_task_while_event_observer_is_active() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    observer_started = asyncio.Event()
    hold_observer = asyncio.Event()

    async def on_event(event: BaseEvent) -> None:
        if event.type.value == "RUN_STARTED" and not observer_started.is_set():
            observer_started.set()
            await hold_observer.wait()

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    consumer = asyncio.create_task(anext(stream))
    await observer_started.wait()

    try:
        abort_tail = await stream.abort()

        assert consumer.cancelled()
        assert [event.type.value for event in abort_tail] == ["RUN_ERROR"]
    finally:
        hold_observer.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_agui_aclose_from_observer_child_task_preserves_current_event() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiEventStream | None = None
    close_task: asyncio.Task[None] | None = None

    async def on_event(_: BaseEvent) -> None:
        nonlocal close_task
        if close_task is None:
            assert stream is not None
            close_task = asyncio.create_task(stream.aclose())
            await asyncio.sleep(0)

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    consumer = asyncio.create_task(anext(stream))
    try:
        consumer_outcome = (await asyncio.gather(consumer, return_exceptions=True))[0]
        assert close_task is not None
        close_outcome = (await asyncio.gather(close_task, return_exceptions=True))[0]

        assert isinstance(consumer_outcome, BaseEvent)
        assert close_outcome is None
    finally:
        await asyncio.gather(consumer, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_agui_abort_from_terminal_observer_is_an_idempotent_noop() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    stream: AgUiEventStream | None = None
    abort_tail: list[BaseEvent] | None = None

    async def on_event(event: BaseEvent) -> None:
        nonlocal abort_tail
        if event.type.value == "RUN_FINISHED":
            assert stream is not None
            abort_tail = await stream.abort()

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert abort_tail == []


@pytest.mark.asyncio
async def test_agui_records_conversion_error_and_emits_one_error_terminal() -> None:
    async def parts() -> AsyncIterator[object]:
        yield {"type": "not-a-stream-mode", "ns": (), "data": {}}

    stream = TinkerFin().run(parts, identity=_identity()).astream_agui()
    events = [event async for event in stream]

    terminals = [event for event in events if isinstance(event, RunErrorEvent)]
    assert len(terminals) == 1
    assert isinstance(stream.error, ValidationError)


@pytest.mark.asyncio
async def test_agui_abort_after_error_terminal_does_not_emit_another_terminal() -> None:
    async def parts() -> AsyncIterator[object]:
        yield {"type": "not-a-stream-mode", "ns": (), "data": {}}

    stream = TinkerFin().run(parts, identity=_identity()).astream_agui()
    events = [event async for event in stream]

    assert len([event for event in events if isinstance(event, RunErrorEvent)]) == 1
    assert await stream.abort() == []


class _BlockingParts:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.release_pull = asyncio.Event()
        self.closed = asyncio.Event()

    def __aiter__(self) -> _BlockingParts:
        return self

    async def __anext__(self) -> object:
        self.pull_started.set()
        await self.release_pull.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.release_pull.set()
        self.closed.set()


class _DeadlineParts:
    def __init__(
        self,
        parts: list[object] | None = None,
        *,
        block_close: bool = False,
    ) -> None:
        self._parts = list(parts or [])
        self._block_close = block_close
        self.pull_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closed = asyncio.Event()

    def __aiter__(self) -> _DeadlineParts:
        return self

    async def __anext__(self) -> object:
        if self._parts:
            return self._parts.pop(0)
        self.pull_started.set()
        await asyncio.Event().wait()
        raise AssertionError("an unreachable blocked pull resumed")

    async def aclose(self) -> None:
        self.close_started.set()
        if self._block_close:
            await self.release_close.wait()
        self.closed.set()


def _message_part(message: object) -> dict[str, object]:
    return {
        "type": "messages",
        "ns": (),
        "data": (
            message,
            {"lc_agent_name": None, "langgraph_node": "model"},
        ),
    }


async def _collect_events(stream: AgUiEventStream) -> list[BaseEvent]:
    return [event async for event in stream]


class _SlowClosingParts:
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closed = asyncio.Event()
        self._yielded = False

    def __aiter__(self) -> _SlowClosingParts:
        return self

    async def __anext__(self) -> object:
        if not self._yielded:
            self._yielded = True
            return {
                "type": "values",
                "ns": (),
                "data": {"value": 1},
                "interrupts": (),
            }
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed.set()


class _InterruptedClosingParts:
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _InterruptedClosingParts:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.close_completed.set()


class _TwoStageFailingCloseParts:
    def __init__(self) -> None:
        self._yielded = False
        self.close_calls = 0

    def __aiter__(self) -> _TwoStageFailingCloseParts:
        return self

    async def __anext__(self) -> object:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return {"type": "not-a-stream-mode", "ns": (), "data": {}}

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise asyncio.CancelledError("close awaitable cancelled itself")
        raise RuntimeError("second close failed")


class _CancelledFailingCloseParts:
    def __init__(self) -> None:
        self._yielded = False
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _CancelledFailingCloseParts:
        return self

    async def __anext__(self) -> object:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return {"type": "not-a-stream-mode", "ns": (), "data": {}}

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        raise RuntimeError("native close failed")


class _BudgetedClosingParts:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _BudgetedClosingParts:
        return self

    async def __anext__(self) -> object:
        self.pull_started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked source unexpectedly resumed")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.closed.set()


@pytest.mark.asyncio
async def test_agui_conversion_error_survives_two_upstream_close_failures() -> None:
    parts = _TwoStageFailingCloseParts()
    stream = AgUiEventStream(
        parts=parts,
        identity=_identity(),
        expose_reasoning_events=False,
        expose_subagent_events=True,
        prior_tool_call_ids=frozenset(),
        timeout=None,
        on_event=None,
    )

    events = await _collect_events(stream)

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(stream.error, ValidationError)
    assert any(
        "CancelledError: close awaitable cancelled itself" in note
        for note in stream.error.__notes__
    )
    assert any(
        "RuntimeError: second close failed" in note for note in stream.error.__notes__
    )
    assert parts.close_calls == 2


@pytest.mark.asyncio
async def test_agui_caller_cancellation_keeps_conversion_and_cleanup_evidence() -> None:
    parts = _CancelledFailingCloseParts()
    stream = TinkerFin().run(lambda: parts, identity=_identity()).astream_agui()
    assert (await anext(stream)).type.value == "RUN_STARTED"
    consumer = asyncio.create_task(anext(stream))
    await parts.close_started.wait()
    consumer.cancel("caller stopped")
    parts.release_close.set()

    try:
        with pytest.raises(asyncio.CancelledError, match="caller stopped") as raised:
            await consumer
        notes = raised.value.__notes__
        assert any("ValidationError" in note for note in notes)
        assert any("RuntimeError: native close failed" in note for note in notes)
        assert isinstance(stream.error, ValidationError)
    finally:
        parts.release_close.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.gather(stream.aclose(), return_exceptions=True)


def test_agui_rejects_invalid_settlement_timeout() -> None:
    async def parts() -> AsyncIterator[object]:
        if False:  # pragma: no cover - only supplies the asynchronous source shape
            yield None

    for invalid in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite and non-negative"):
            TinkerFin().run(parts, identity=_identity()).astream_agui(
                settlement_timeout=invalid,
            )


@pytest.mark.asyncio
async def test_agui_settlement_timeout_retains_close_for_a_second_waiter() -> None:
    parts = _BudgetedClosingParts()
    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            settlement_timeout=0.01,
        )
    )
    assert (await anext(stream)).type.value == "RUN_STARTED"
    active_pull = asyncio.create_task(anext(stream))
    await parts.pull_started.wait()

    try:
        with pytest.raises(TimeoutError, match="settlement timed out") as raised:
            await stream.aclose()
        assert type(raised.value).__name__ == "AgUiSettlementTimeoutError"
        assert parts.close_started.is_set()
        assert not parts.close_cancelled.is_set()
        assert not parts.closed.is_set()

        parts.release_close.set()
        await stream.aclose()
    finally:
        parts.release_close.set()
        await asyncio.gather(active_pull, return_exceptions=True)
        await asyncio.gather(stream.aclose(), return_exceptions=True)

    assert parts.closed.is_set()
    assert parts.close_calls == 1
    assert active_pull.cancelled()


@pytest.mark.asyncio
async def test_agui_abort_cancels_active_pull_and_returns_observed_tail_once() -> None:
    parts = _BlockingParts()
    observed: list[str] = []

    async def on_event(event: BaseEvent) -> None:
        observed.append(event.type.value)

    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    started = await anext(stream)
    assert started.type.value == "RUN_STARTED"
    pending = asyncio.create_task(anext(stream))
    await parts.pull_started.wait()

    try:
        tail = await stream.abort()
    finally:
        if not pending.done():
            pending.cancel()
        outcome = (await asyncio.gather(pending, return_exceptions=True))[0]
        await stream.aclose()

    assert isinstance(outcome, asyncio.CancelledError)
    assert parts.closed.is_set()
    assert [event.type.value for event in tail] == ["RUN_ERROR"]
    terminal = tail[0]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "cancelled"
    assert observed == ["RUN_STARTED", "RUN_ERROR"]
    assert await stream.abort() == []


@pytest.mark.asyncio
async def test_agui_zero_timeout_starts_lifecycle_without_pulling_parts() -> None:
    pulls = 0

    async def parts() -> AsyncIterator[object]:
        nonlocal pulls
        pulls += 1
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            timeout=0,
        )
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"
    assert isinstance(stream.error, TimeoutError)
    assert pulls == 0


@pytest.mark.asyncio
async def test_agui_total_deadline_closes_text_before_timeout_terminal() -> None:
    parts = _DeadlineParts(
        [_message_part(AIMessageChunk(id="message-timeout", content="partial"))]
    )
    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            timeout=0.01,
        )
    )

    events = await _collect_events(stream)

    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_ERROR",
    ]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"
    assert isinstance(stream.error, TimeoutError)
    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_agui_total_deadline_closes_parallel_tools_before_timeout_terminal() -> (
    None
):
    parts = _DeadlineParts(
        [
            _message_part(
                AIMessageChunk(
                    id="message-tools-timeout",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "search",
                            "args": '{"query":"first"}',
                            "id": "call-first",
                            "index": 0,
                            "type": "tool_call_chunk",
                        },
                        {
                            "name": "search",
                            "args": '{"query":"second"}',
                            "id": "call-second",
                            "index": 1,
                            "type": "tool_call_chunk",
                        },
                    ],
                )
            )
        ]
    )
    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            timeout=0.01,
        )
    )

    events = await _collect_events(stream)
    event_types = [event.type.value for event in events]

    assert event_types == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_END",
        "RUN_ERROR",
    ]
    assert event_types.count("RUN_ERROR") == 1
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"


@pytest.mark.asyncio
async def test_agui_total_deadline_before_interrupt_uses_timeout_terminal() -> None:
    parts = _DeadlineParts()
    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            timeout=0.01,
        )
    )

    events = await _collect_events(stream)

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"


@pytest.mark.asyncio
async def test_agui_total_deadline_after_interrupt_does_not_finish_the_run() -> None:
    parts = _DeadlineParts(
        [
            {
                "type": "values",
                "ns": (),
                "data": {"messages": [], "phase": "awaiting-input"},
                "interrupts": ({"id": "pause", "value": {"pause": True}},),
            }
        ]
    )
    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            timeout=0.01,
        )
    )

    events = await _collect_events(stream)
    event_types = [event.type.value for event in events]

    assert event_types == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "MESSAGES_SNAPSHOT",
        "RUN_ERROR",
    ]
    assert "RUN_FINISHED" not in event_types
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"


@pytest.mark.asyncio
async def test_agui_timeout_waits_for_owned_upstream_close_before_terminal() -> None:
    parts = _DeadlineParts(block_close=True)
    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            timeout=0.01,
        )
    )
    collecting = asyncio.create_task(_collect_events(stream))
    await asyncio.wait_for(parts.close_started.wait(), timeout=1)

    try:
        assert not collecting.done()
        parts.release_close.set()
        events = await asyncio.wait_for(collecting, timeout=1)
    finally:
        parts.release_close.set()
        await asyncio.gather(collecting, return_exceptions=True)
        await stream.aclose()

    assert parts.closed.is_set()
    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "stream_timeout"


@pytest.mark.asyncio
async def test_upstream_timeout_error_remains_a_runtime_error() -> None:
    async def parts() -> AsyncIterator[object]:
        raise TimeoutError("provider request timed out")
        yield  # pragma: no cover - keeps the function an async generator

    stream = (
        TinkerFin()
        .run(parts, identity=_identity())
        .astream_agui(
            timeout=1,
        )
    )

    events = await _collect_events(stream)

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "runtime_error"
    assert isinstance(stream.error, TimeoutError)
    assert str(stream.error) == "provider request timed out"


@pytest.mark.asyncio
async def test_agui_waits_for_cleanup_when_consumer_is_cancelled_during_failure() -> (
    None
):
    parts = _SlowClosingParts()

    async def on_event(event: BaseEvent) -> None:
        if event.type.value == "STATE_SNAPSHOT":
            raise RuntimeError("event observer failed")

    stream = (
        TinkerFin()
        .run(lambda: parts, identity=_identity())
        .astream_agui(
            on_event=on_event,
        )
    )
    assert (await anext(stream)).type.value == "RUN_STARTED"
    consumer = asyncio.create_task(anext(stream))
    await parts.close_started.wait()
    consumer.cancel("request cancelled during AG-UI cleanup")

    try:
        await asyncio.sleep(0)
        assert not consumer.done()
        parts.release_close.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="request cancelled during AG-UI cleanup",
        ):
            await consumer
    finally:
        parts.release_close.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()

    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_agui_cancelled_terminal_pull_waits_for_upstream_close() -> None:
    parts = _InterruptedClosingParts()
    stream = TinkerFin().run(lambda: parts, identity=_identity()).astream_agui()
    assert (await anext(stream)).type.value == "RUN_STARTED"
    terminal_pull = asyncio.create_task(anext(stream))
    await parts.close_started.wait()

    terminal_pull.cancel("request cancelled during native parts close")
    await asyncio.sleep(0)
    assert not terminal_pull.done()
    assert not parts.close_cancelled.is_set()
    parts.release_close.set()

    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="request cancelled during native parts close",
        ):
            await terminal_pull
        await stream.aclose()
    finally:
        parts.release_close.set()
        await asyncio.gather(terminal_pull, return_exceptions=True)

    assert parts.close_calls == 1
    assert parts.close_completed.is_set()
