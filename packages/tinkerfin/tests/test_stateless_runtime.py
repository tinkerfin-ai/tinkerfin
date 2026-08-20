from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import RunControl
from langgraph.types import StreamMode

import tinkerfin.coordination as coordination
from tinkerfin import GraphRunStream, Identity, TinkerFin


def _identity() -> Identity:
    return Identity(threadId="thread-1", runId="run-1")


class _GraphState(TypedDict, total=False):
    messages: list[object]
    n: int


class _GraphContext(TypedDict, total=False):
    tenant: str


class _GraphInput(TypedDict, total=False):
    messages: list[object]


class RecordingGraph:
    def __init__(
        self,
        parts: list[object],
        *,
        pull_gate: asyncio.Event | None = None,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.parts = parts
        self.pull_gate = pull_gate
        self.close_gate = close_gate
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.closed = asyncio.Event()

    async def astream(
        self,
        input: object,
        config: object | None = None,
        *,
        context: object | None = None,
        stream_mode: StreamMode | tuple[StreamMode, ...] | None = None,
        print_mode: StreamMode | tuple[StreamMode, ...] = (),
        output_keys: str | tuple[str, ...] | None = None,
        interrupt_before: str | tuple[str, ...] | None = None,
        interrupt_after: str | tuple[str, ...] | None = None,
        durability: str | None = None,
        control: object | None = None,
        subgraphs: bool = False,
        debug: bool | None = None,
        version: str = "v1",
        **kwargs: object,
    ) -> AsyncIterator[object]:
        self.calls.append(
            {
                "input": input,
                "config": config,
                "context": context,
                "stream_mode": stream_mode,
                "print_mode": print_mode,
                "output_keys": output_keys,
                "interrupt_before": interrupt_before,
                "interrupt_after": interrupt_after,
                "durability": durability,
                "control": control,
                "subgraphs": subgraphs,
                "debug": debug,
                "version": version,
                "kwargs": kwargs,
            }
        )
        self.started.set()
        try:
            if self.pull_gate is not None:
                await self.pull_gate.wait()
            for part in self.parts:
                yield part
        finally:
            self.close_started.set()
            if self.close_gate is not None:
                await self.close_gate.wait()
            self.closed.set()


@pytest.mark.asyncio
async def test_run_stream_forwards_native_arguments_and_observes_before_delivery() -> (
    None
):
    graph = RecordingGraph([{"type": "values", "ns": (), "data": {"n": 1}}])
    order: list[tuple[str, object]] = []

    async def on_part(part: object) -> None:
        order.append(("observed", part))

    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    control = RunControl()
    runtime = TinkerFin().run(
        lambda: graph.astream(
            {"messages": []},
            config,
            context={"tenant": "tenant-1"},
            stream_mode=("messages", "tasks", "values"),
            print_mode="debug",
            output_keys=("messages",),
            interrupt_before=("agent",),
            interrupt_after=("tools",),
            durability="sync",
            control=control,
            subgraphs=True,
            debug=False,
            version="v2",
            custom_flag="preserved",
        ),
        on_part=on_part,
    )

    assert type(runtime).__name__ == "TinkerFinRun"
    stream = runtime.astream()
    part = await anext(stream)
    order.append(("delivered", part))
    await stream.aclose()

    assert order == [("observed", part), ("delivered", part)]
    assert graph.calls == [
        {
            "input": {"messages": []},
            "config": config,
            "context": {"tenant": "tenant-1"},
            "stream_mode": ("messages", "tasks", "values"),
            "print_mode": "debug",
            "output_keys": ("messages",),
            "interrupt_before": ("agent",),
            "interrupt_after": ("tools",),
            "durability": "sync",
            "control": control,
            "subgraphs": True,
            "debug": False,
            "version": "v2",
            "kwargs": {"custom_flag": "preserved"},
        }
    ]
    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_graph_aclose_from_observer_child_task_preserves_current_part() -> None:
    graph = RecordingGraph([{"type": "values", "ns": (), "data": {"n": 1}}])
    stream: GraphRunStream[object] | None = None
    close_task: asyncio.Task[None] | None = None

    async def on_part(_: object) -> None:
        nonlocal close_task
        if close_task is None:
            assert stream is not None
            close_task = asyncio.create_task(stream.aclose())
            await asyncio.sleep(0)

    stream = (
        TinkerFin()
        .run(
            lambda: graph.astream({}, version="v2"),
            on_part=on_part,
        )
        .astream()
    )
    consumer = asyncio.create_task(anext(stream))
    try:
        consumer_outcome = (await asyncio.gather(consumer, return_exceptions=True))[0]
        assert close_task is not None
        close_outcome = (await asyncio.gather(close_task, return_exceptions=True))[0]

        assert consumer_outcome == {"type": "values", "ns": (), "data": {"n": 1}}
        assert close_outcome is None
    finally:
        await asyncio.gather(consumer, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await stream.aclose()

    assert graph.closed.is_set()


def test_identity_contract_matches_coordinator_configuration() -> None:
    graph = RecordingGraph([])
    plain = TinkerFin()

    run = plain.run(lambda: graph.astream({}, version="v2"), identity=_identity())
    assert run._identity == _identity()

    coordinated = TinkerFin(
        run_coordinator=coordination.InMemoryRunCoordinator(
            key_resolver=lambda value: value.thread_id
        )
    )
    with pytest.raises(ValueError, match="identity"):
        coordinated.run(lambda: graph.astream({}, version="v2"))


@pytest.mark.asyncio
async def test_coordinator_is_lazy_and_released_when_stream_closes() -> None:
    entered = asyncio.Event()
    released = asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: Identity) -> AsyncIterator[None]:
        assert identity == _identity()
        entered.set()
        try:
            yield
        finally:
            released.set()

    graph = RecordingGraph([{"type": "values", "ns": (), "data": {}}])
    stream = (
        TinkerFin(run_coordinator=coordinate)
        .run(lambda: graph.astream({}, version="v2"), identity=_identity())
        .astream()
    )

    assert not entered.is_set()
    assert not graph.started.is_set()
    await anext(stream)
    assert entered.is_set()
    assert graph.started.is_set()

    await stream.aclose()

    assert released.is_set()
    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_closing_stream_cancels_an_active_graph_pull_before_releasing() -> None:
    released = asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: Identity) -> AsyncIterator[None]:
        del identity
        try:
            yield
        finally:
            released.set()

    graph = RecordingGraph(
        [{"type": "values", "ns": (), "data": {}}],
        pull_gate=asyncio.Event(),
    )
    stream = (
        TinkerFin(run_coordinator=coordinate)
        .run(lambda: graph.astream({}, version="v2"), identity=_identity())
        .astream()
    )
    pull = asyncio.create_task(anext(stream))
    await graph.started.wait()

    try:
        await stream.aclose()
    finally:
        if not pull.done():
            pull.cancel()
        outcome = (await asyncio.gather(pull, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert graph.closed.is_set()
    assert released.is_set()


@pytest.mark.asyncio
async def test_concurrent_close_waits_for_the_same_upstream_cleanup() -> None:
    close_gate = asyncio.Event()
    graph = RecordingGraph(
        [{"type": "values", "ns": (), "data": {}}],
        close_gate=close_gate,
    )
    stream = TinkerFin().run(lambda: graph.astream({}, version="v2")).astream()
    await anext(stream)

    first_close = asyncio.create_task(stream.aclose())
    await graph.close_started.wait()
    second_close = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0)
    try:
        assert not second_close.done()
    finally:
        close_gate.set()
        await asyncio.gather(first_close, second_close)

    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_upstream_failure_is_not_replaced_by_source_close_failure() -> None:
    class FailingSource:
        def __aiter__(self) -> FailingSource:
            return self

        async def __anext__(self) -> object:
            raise ValueError("graph failed")

        async def aclose(self) -> None:
            raise RuntimeError("source close failed")

    stream = TinkerFin().run(FailingSource).astream()

    with pytest.raises(ValueError, match="graph failed"):
        await anext(stream)


@pytest.mark.asyncio
async def test_graph_stream_waits_for_cleanup_when_close_caller_is_cancelled() -> None:
    close_gate = asyncio.Event()
    graph = RecordingGraph(
        [{"type": "values", "ns": (), "data": {}}],
        close_gate=close_gate,
    )
    stream = TinkerFin().run(lambda: graph.astream({}, version="v2")).astream()
    await anext(stream)
    closing = asyncio.create_task(stream.aclose())
    await graph.close_started.wait()
    closing.cancel("request cancelled while Graph cleanup was running")

    try:
        await asyncio.sleep(0)
        assert not closing.done()
        close_gate.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="request cancelled while Graph cleanup was running",
        ):
            await closing
    finally:
        close_gate.set()
        await asyncio.gather(closing, return_exceptions=True)
        await stream.aclose()

    assert graph.closed.is_set()
