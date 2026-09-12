"""Runtime and durable SSE bodies work with ordinary HTTP response classes."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable

import pytest
from langchain.agents.middleware.types import InputAgentState
from langgraph.graph.state import CompiledStateGraph
from sse_starlette.sse import EventSourceResponse
from starlette.responses import StreamingResponse
from starlette.types import Message, Scope

from tinkerfin import AgentRuntime, SsePayload
from tinkerfin.sse import encode_sse_payload
from tinkerfin_messaging import Messaging


class _Graph:
    async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield {"type": "values", "ns": (), "data": {"answer": "你好"}, "interrupts": ()}


setattr(_Graph.astream, "__signature__", inspect.signature(CompiledStateGraph.astream))


async def _send_body(body: AsyncIterator[str | bytes], response_kind: str) -> bytes:
    chunks: list[bytes] = []

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    response = (
        StreamingResponse(body, media_type="text/event-stream")
        if response_kind == "streaming"
        else EventSourceResponse(body, ping=0)
    )
    scope: Scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
    await response(scope, receive, send)
    return b"".join(chunks)


@pytest.mark.parametrize("response_kind", ["streaming", "event_source"])
@pytest.mark.parametrize("source_kind", ["native", "agui", "messaging", "subscription"])
async def test_http_responses_send_object_events_without_double_encoding(
    definition_factory: Callable[..., AgentRuntime[None]],
    response_kind: str,
    source_kind: str,
) -> None:
    runtime = definition_factory(_Graph())
    if source_kind == "native":
        stream = runtime.open_run(
            thread_id="thread", run_id="run", input=InputAgentState(messages=[])
        )
    else:
        stream = runtime.open_agui_run(
            thread_id="thread", run_id="run", input=InputAgentState(messages=[])
        )
    async with Messaging() as messaging:
        channel = messaging.channel(name="events")
        if source_kind == "messaging":
            body = await channel.open_sse(stream)
        elif source_kind == "subscription":
            subscription = await channel.wrap(stream)
            body = subscription.to_sse()
        else:
            body = stream.to_sse()
        try:
            wire = await _send_body(body, response_kind)
        finally:
            await body.aclose()
    frames = wire.replace(b"\r\n", b"\n").strip().split(b"\n\n")
    events = [
        json.loads(
            b"\n".join(
                line[6:] for line in frame.split(b"\n") if line.startswith(b"data: ")
            )
        )
        for frame in frames
    ]
    if source_kind == "native":
        assert events[0]["data"]["state"]["answer"] == "你好"
        assert wire.startswith(b"event: stream-part\n")
    else:
        assert [event["type"] for event in events] == [
            "RUN_STARTED",
            "STATE_SNAPSHOT",
            "RUN_FINISHED",
        ]
        assert events[1]["snapshot"]["answer"] == "你好"
        if source_kind in {"messaging", "subscription"}:
            assert [frame.splitlines()[0] for frame in frames] == [
                b"id: 1",
                b"id: 2",
                b"id: 3",
            ]


@pytest.mark.parametrize("response_kind", ["streaming", "event_source"])
async def test_http_response_preserves_custom_utf8_sse_fields(
    definition_factory: Callable[..., AgentRuntime[None]], response_kind: str
) -> None:
    async def payload(_event: object) -> SsePayload:
        return SsePayload(event="progress", data="中文\r\nnext\rlast", retry=1500)

    async def event_id(_event: object) -> str:
        return "事件-1"

    stream = definition_factory(_Graph()).open_run(
        thread_id="thread", run_id="run", input=InputAgentState(messages=[])
    )
    body = stream.to_sse(mapper=payload, event_id_resolver=event_id)
    try:
        wire = await _send_body(body, response_kind)
    finally:
        await body.aclose()
    assert (
        wire
        == "id: 事件-1\nevent: progress\nretry: 1500\ndata: 中文\ndata: next\ndata: last\n\n".encode()
    )


def test_sse_payload_is_an_encoded_utf8_frame() -> None:
    assert encode_sse_payload(SsePayload(data="中文")) == "data: 中文\n\n".encode()


async def test_event_source_response_accepts_custom_encoding_without_to_sse(
    definition_factory: Callable[..., AgentRuntime[None]],
) -> None:
    stream = definition_factory(_Graph()).open_run(
        thread_id="thread", run_id="run", input=InputAgentState(messages=[])
    )

    async def events() -> AsyncIterator[dict[str, str]]:
        try:
            async for part in stream:
                assert isinstance(part, dict)
                yield {"event": "application", "data": "custom"}
        finally:
            await stream.aclose()

    chunks: list[bytes] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    await EventSourceResponse(events(), ping=0)(
        {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
        receive,
        send,
    )
    assert b"".join(chunks) == b"event: application\r\ndata: custom\r\n\r\n"
