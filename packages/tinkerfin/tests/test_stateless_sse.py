from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
from ag_ui.core import BaseEvent
from langchain.agents.middleware.types import InputAgentState
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from tinkerfin import (
    DeepAgentDefinition,
    NativeStreamPart,
    RunIdentity,
    SseBody,
    SseMapper,
    SsePayload,
)


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


class _CountingParts:
    def __init__(self) -> None:
        self.pulls = 0
        self.closed = asyncio.Event()

    def __aiter__(self) -> _CountingParts:
        return self

    async def __anext__(self) -> object:
        self.pulls += 1
        if self.pulls == 1:
            return {
                "type": "values",
                "ns": (),
                "data": {"value": 1},
                "interrupts": (),
            }
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed.set()


class _BlockingParts:
    def __init__(self) -> None:
        self.pull_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> _BlockingParts:
        return self

    async def __anext__(self) -> object:
        self.pull_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


@pytest.mark.asyncio
async def test_prepare_does_not_observe_or_resolve_an_event_id(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    parts = _CountingParts()
    order: list[str] = []

    async def preflight() -> None:
        order.append("preflight")

    async def on_event(event: BaseEvent) -> None:
        order.append(f"observe:{event.type.value}")

    async def event_id_resolver(event: BaseEvent) -> str:
        order.append(f"resolve:{event.type.value}")
        return f"event-{len(order)}"

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .new_agui(
            identity=_identity(),
            on_event=on_event,
        )
        .astream(_graph_input())
        .to_sse(event_id_resolver=event_id_resolver)
    )

    await body.prepare(preflight=preflight)

    assert order == ["preflight"]
    assert parts.pulls == 0

    first = await anext(body)

    assert first.startswith("id: event-3\n")
    assert '"type":"RUN_STARTED"' in first
    assert order == [
        "preflight",
        "observe:RUN_STARTED",
        "resolve:RUN_STARTED",
    ]
    assert parts.pulls == 0

    remaining = [frame async for frame in body]
    assert len(remaining) == 2
    assert parts.pulls == 2
    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_preflight_failure_closes_the_claimed_body_without_calling_factory(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    factory_calls = 0

    async def source() -> AsyncIterator[object]:
        nonlocal factory_calls
        factory_calls += 1
        if False:  # pragma: no cover - preflight must not open this source
            yield None

    body = (
        definition_factory(_SourceGraph(source))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse()
    )

    async def preflight() -> None:
        raise RuntimeError("request is no longer authorized")

    with pytest.raises(RuntimeError, match="no longer authorized"):
        await body.prepare(preflight=preflight)

    assert factory_calls == 0
    with pytest.raises(StopAsyncIteration):
        await anext(body)


@pytest.mark.asyncio
async def test_native_custom_mapper_filters_and_controls_payload_fields(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {"value": 1}, "interrupts": ()}
        yield {"type": "values", "ns": (), "data": {"value": 2}, "interrupts": ()}

    async def mapper(part: NativeStreamPart) -> SsePayload | None:
        if part.data == {
            "state": {"value": 1},
            "messages": [],
            "interrupts": [],
        }:
            return None
        return SsePayload(data="line-1\nline-2", event="custom", retry=1500)

    async def event_id_resolver(part: NativeStreamPart) -> int:
        assert part.data == {
            "state": {"value": 2},
            "messages": [],
            "interrupts": [],
        }
        return 7

    body = (
        definition_factory(_SourceGraph(source))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse(
            mapper=mapper,
            event_id_resolver=event_id_resolver,
        )
    )

    assert [frame async for frame in body] == [
        "id: 7\nevent: custom\nretry: 1500\ndata: line-1\ndata: line-2\n\n"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected_frame"),
    [
        ("line\n", "data: line\ndata: \n\n"),
        (
            "first\r\nsecond\rthird",
            "data: first\ndata: second\ndata: third\n\n",
        ),
        ("left\u2028right", "data: left\u2028right\n\n"),
    ],
)
async def test_custom_sse_data_preserves_exact_sse_line_semantics(
    data: str,
    expected_frame: str,
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    async def mapper(_part: NativeStreamPart) -> SsePayload:
        return SsePayload(data=data)

    body = (
        definition_factory(_SourceGraph(source))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse(mapper=mapper)
    )

    assert [frame async for frame in body] == [expected_frame]


def test_sse_payload_rejects_boolean_retry_and_multiline_event_name() -> None:
    with pytest.raises(ValidationError):
        SsePayload(data="value", retry=True)
    with pytest.raises(ValidationError, match="line breaks"):
        SsePayload(data="value", event="bad\nevent")


@pytest.mark.asyncio
async def test_custom_mapper_must_be_async_and_return_sse_payload(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}

    def synchronous_mapper(_: NativeStreamPart) -> SsePayload:
        return SsePayload(data="invalid")

    body = (
        definition_factory(_SourceGraph(source))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse(mapper=cast(SseMapper[NativeStreamPart], synchronous_mapper))
    )

    with pytest.raises(TypeError, match="mapper must return an awaitable"):
        await anext(body)


@pytest.mark.asyncio
async def test_agui_mapper_receives_validated_event_objects(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    async def source() -> AsyncIterator[object]:
        if False:
            yield None

    seen: list[str] = []

    async def mapper(event: BaseEvent) -> SsePayload:
        seen.append(event.type.value)
        return SsePayload(data=json.dumps({"kind": event.type.value}))

    body: SseBody[str] = (
        definition_factory(_SourceGraph(source))
        .new_agui(identity=_identity())
        .astream(_graph_input())
        .to_sse(mapper=mapper)
    )

    frames = [frame async for frame in body]

    assert seen == ["RUN_STARTED", "RUN_FINISHED"]
    assert [
        json.loads(frame.removeprefix("data: "))["kind"] for frame in frames
    ] == seen


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", [True, "bad\nid", "bad\x00id", object()])
async def test_event_id_resolver_rejects_unsafe_or_unsupported_ids(
    event_id: object,
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    parts = _CountingParts()

    async def event_id_resolver(
        _part: NativeStreamPart,
    ) -> str | int | None:
        return cast(str | int | None, event_id)

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse(
            event_id_resolver=event_id_resolver,
        )
    )

    with pytest.raises(
        TypeError if event_id is True or not isinstance(event_id, str) else ValueError
    ):
        await anext(body)

    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_native_sse_timeout_covers_mapper_and_closes_upstream(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    parts = _CountingParts()

    async def mapper(_part: NativeStreamPart) -> SsePayload:
        await asyncio.sleep(0.05)
        return SsePayload(data="too late")

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse(
            timeout=0.001,
            mapper=mapper,
        )
    )

    with pytest.raises(TimeoutError, match="native SSE stream timed out"):
        await anext(body)

    assert parts.closed.is_set()


@pytest.mark.asyncio
async def test_external_sse_close_cancels_an_active_pull_and_is_idempotent(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    parts = _BlockingParts()
    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse()
    )
    pull = asyncio.create_task(anext(body))
    await parts.pull_started.wait()

    await body.aclose()
    await body.aclose()

    assert (await asyncio.gather(pull, return_exceptions=True))[
        0
    ].__class__ is asyncio.CancelledError
    assert parts.closed.is_set()
    assert parts.close_calls == 1


@pytest.mark.asyncio
async def test_mapper_failure_closes_upstream_and_preserves_the_primary_error(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    parts = _CountingParts()

    async def mapper(_part: NativeStreamPart) -> SsePayload:
        raise LookupError("cannot map part")

    body = (
        definition_factory(_SourceGraph(lambda: parts))
        .new(identity=_identity())
        .astream(_graph_input())
        .to_sse(mapper=mapper)
    )

    with pytest.raises(LookupError, match="cannot map part"):
        await anext(body)

    assert parts.closed.is_set()
