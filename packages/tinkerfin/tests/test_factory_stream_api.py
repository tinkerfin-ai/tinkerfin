from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
from ag_ui.core import BaseEvent
from langchain.agents.middleware.types import InputAgentState
from langgraph.graph.state import CompiledStateGraph

from tinkerfin import DeepAgentDefinition, RunIdentity


def _identity() -> RunIdentity:
    return RunIdentity(threadId="thread-1", runId="run-1")


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


class _SourceGraph:
    def __init__(self, source_factory: Callable[[], AsyncIterator[object]]) -> None:
        self._source_factory = source_factory

    def astream(
        self,
        *_args: object,
        **_options: object,
    ) -> AsyncIterator[object]:
        return self._source_factory()


setattr(
    _SourceGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


@pytest.mark.asyncio
async def test_native_facade_binds_one_lazy_graph_source(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    calls = 0
    closed = False
    observed: list[object] = []
    part = {
        "type": "values",
        "ns": (),
        "data": {"answer": 42},
        "interrupts": (),
    }

    async def source() -> AsyncIterator[object]:
        nonlocal calls, closed
        calls += 1
        try:
            yield part
        finally:
            closed = True

    async def on_part(value: object) -> None:
        observed.append(value)

    runtime = definition_factory(_SourceGraph(source)).new(
        identity=_identity(),
        on_part=on_part,
    )
    stream = runtime.astream(_graph_input())

    assert calls == 0
    assert await anext(stream) is part
    assert calls == 1
    assert observed == [part]

    await stream.aclose()

    assert closed is True


def test_native_facade_allows_exactly_one_stream_claim(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        if False:  # pragma: no cover - provides the source shape only
            yield None

    runtime = definition_factory(_SourceGraph(source)).new(identity=_identity())

    runtime.astream(_graph_input())

    with pytest.raises(RuntimeError, match="one object stream"):
        runtime.astream(_graph_input())


@pytest.mark.asyncio
async def test_astream_agui_converts_the_bound_factory_without_a_parts_argument(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {
            "type": "values",
            "ns": (),
            "data": {"answer": 42},
            "interrupts": (),
        }

    observed: list[BaseEvent] = []

    async def on_event(event: BaseEvent) -> None:
        observed.append(event)

    runtime = definition_factory(_SourceGraph(source)).new_agui(
        identity=_identity(),
        on_event=on_event,
    )
    events = runtime.astream(_graph_input())
    delivered = [event async for event in events]

    assert [event.type.value for event in delivered] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert observed == delivered


@pytest.mark.asyncio
async def test_native_facade_rejects_a_graph_result_without_async_iteration(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    def source() -> AsyncIterator[object]:
        return cast(AsyncIterator[object], object())

    stream = (
        definition_factory(_SourceGraph(source))
        .new(identity=_identity())
        .astream(_graph_input())
    )

    with pytest.raises(
        TypeError,
        match="source_factory must return an async iterator",
    ):
        await anext(stream)
