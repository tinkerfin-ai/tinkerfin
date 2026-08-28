from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import RunControl
from langgraph.types import StreamMode

from tinkerfin import DeepAgentDefinition, NativeGraphRunStream, RunIdentity, TinkerFin


def _identity() -> RunIdentity:
    return RunIdentity(threadId="thread-1", runId="run-1")


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


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
async def test_run_stream_forwards_native_arguments_and_observes_before_delivery(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    graph = RecordingGraph([{"type": "values", "ns": (), "data": {"n": 1}}])
    order: list[tuple[str, object]] = []

    async def on_part(part: object) -> None:
        order.append(("observed", part))

    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    control = RunControl()
    runtime = definition_factory(graph).new(
        identity=_identity(),
        on_part=on_part,
    )
    stream = runtime.astream(
        _graph_input(),
        config,
        stream_mode=("messages", "tasks", "values"),
        print_mode="debug",
        output_keys=None,
        interrupt_before=("agent",),
        interrupt_after=("tools",),
        durability="sync",
        control=control,
        subgraphs=True,
        debug=False,
        version="v2",
    )

    assert isinstance(stream, NativeGraphRunStream)
    part = await anext(stream)
    order.append(("delivered", part))
    await stream.aclose()

    assert order == [("observed", part), ("delivered", part)]
    assert graph.calls == [
        {
            "input": {"messages": []},
            "config": {
                "configurable": {
                    "thread_id": "thread-1",
                    "_tinkerfin_runtime_profile": "deepagents-v2",
                }
            },
            "context": None,
            "stream_mode": ("messages", "tasks", "values"),
            "print_mode": "debug",
            "output_keys": None,
            "interrupt_before": ("agent",),
            "interrupt_after": ("tools",),
            "durability": "sync",
            "control": control,
            "subgraphs": True,
            "debug": False,
            "version": "v2",
            "kwargs": {},
        }
    ]
    assert config == {"configurable": {"thread_id": "thread-1"}}
    assert graph.closed.is_set()


@pytest.mark.asyncio
async def test_graph_aclose_from_observer_child_task_preserves_current_part(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    graph = RecordingGraph([{"type": "values", "ns": (), "data": {"n": 1}}])
    stream: NativeGraphRunStream | None = None
    close_task: asyncio.Task[None] | None = None

    async def on_part(_: object) -> None:
        nonlocal close_task
        if close_task is None:
            assert stream is not None
            close_task = asyncio.create_task(stream.aclose())
            await asyncio.sleep(0)

    stream = (
        definition_factory(graph)
        .new(
            identity=_identity(),
            on_part=on_part,
        )
        .astream(
            _graph_input(),
            version="v2",
        )
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


def test_native_stream_exposes_the_requested_identity(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    graph = RecordingGraph([])
    stream = definition_factory(graph).new(identity=_identity()).astream(_graph_input())

    assert stream.messaging_identity == _identity()


@pytest.mark.asyncio
async def test_coordinator_is_lazy_and_released_when_stream_closes(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    entered = asyncio.Event()
    released = asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        assert identity == _identity()
        entered.set()
        try:
            yield
        finally:
            released.set()

    graph = RecordingGraph([{"type": "values", "ns": (), "data": {}}])
    stream = (
        definition_factory(
            graph,
            tinkerfin=TinkerFin(run_coordinator=coordinate),
        )
        .new(identity=_identity())
        .astream(_graph_input())
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
async def test_closing_stream_cancels_an_active_graph_pull_before_releasing(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    released = asyncio.Event()

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
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
        definition_factory(
            graph,
            tinkerfin=TinkerFin(run_coordinator=coordinate),
        )
        .new(identity=_identity())
        .astream(_graph_input())
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
async def test_concurrent_close_waits_for_the_same_upstream_cleanup(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    close_gate = asyncio.Event()
    graph = RecordingGraph(
        [{"type": "values", "ns": (), "data": {}}],
        close_gate=close_gate,
    )
    stream = definition_factory(graph).new(identity=_identity()).astream(_graph_input())
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
async def test_upstream_failure_is_not_replaced_by_source_close_failure(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    class FailingSource:
        def __aiter__(self) -> FailingSource:
            return self

        async def __anext__(self) -> object:
            raise ValueError("graph failed")

        async def aclose(self) -> None:
            raise RuntimeError("source close failed")

    class FailingGraph:
        def astream(
            self,
            *_args: object,
            **_options: object,
        ) -> AsyncIterator[object]:
            return FailingSource()

    stream = (
        definition_factory(FailingGraph())
        .new(identity=_identity())
        .astream(_graph_input())
    )

    with pytest.raises(ValueError, match="graph failed"):
        await anext(stream)


@pytest.mark.asyncio
async def test_graph_stream_waits_for_cleanup_when_close_caller_is_cancelled(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    close_gate = asyncio.Event()
    graph = RecordingGraph(
        [{"type": "values", "ns": (), "data": {}}],
        close_gate=close_gate,
    )
    stream = definition_factory(graph).new(identity=_identity()).astream(_graph_input())
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
