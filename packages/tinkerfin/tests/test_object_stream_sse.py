from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from ag_ui.core import RunAgentInput

from tinkerfin import TinkerFin


def _run_input() -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )


@pytest.mark.asyncio
async def test_native_object_stream_encodes_default_versioned_sse() -> None:
    async def source() -> AsyncIterator[object]:
        yield {
            "type": "values",
            "ns": ("child:task-1",),
            "data": {"answer": 42},
            "interrupts": (),
        }

    body = TinkerFin().run(source).astream().to_sse()
    frames = [frame async for frame in body]

    assert len(frames) == 1
    assert frames[0].startswith("event: stream-part\n")
    payload = json.loads(frames[0].split("data: ", maxsplit=1)[1])
    assert payload == {
        "schemaVersion": 1,
        "type": "values",
        "ns": ["child:task-1"],
        "data": {"answer": 42},
        "interrupts": [],
    }


@pytest.mark.parametrize(
    "mode",
    [
        "messages",
        "tasks",
        "values",
        "updates",
        "checkpoints",
        "debug",
        "custom",
    ],
)
@pytest.mark.asyncio
async def test_native_object_stream_encodes_every_supported_v2_mode(
    mode: str,
) -> None:
    async def source() -> AsyncIterator[object]:
        yield {"type": mode, "ns": (), "data": {"mode": mode}}

    frames = [frame async for frame in TinkerFin().run(source).astream().to_sse()]

    assert len(frames) == 1
    payload = json.loads(frames[0].split("data: ", maxsplit=1)[1])
    assert payload == {
        "schemaVersion": 1,
        "type": mode,
        "ns": [],
        "data": {"mode": mode},
        "interrupts": [],
    }


@pytest.mark.asyncio
async def test_agui_object_stream_encodes_protocol_json_without_event_name() -> None:
    async def source() -> AsyncIterator[object]:
        if False:  # pragma: no cover - produces only lifecycle events
            yield None

    body = (
        TinkerFin()
        .run(source)
        .astream_agui(
            run_input=_run_input(),
        )
        .to_sse()
    )
    frames = [frame async for frame in body]

    assert [json.loads(frame.removeprefix("data: "))["type"] for frame in frames] == [
        "RUN_STARTED",
        "RUN_FINISHED",
    ]
    assert all("event:" not in frame for frame in frames)


@pytest.mark.asyncio
async def test_sse_prepare_runs_preflight_without_opening_the_source() -> None:
    factory_calls = 0
    preflights = 0

    async def source() -> AsyncIterator[object]:
        nonlocal factory_calls
        factory_calls += 1
        if False:  # pragma: no cover - validates lazy preparation
            yield None

    async def preflight() -> None:
        nonlocal preflights
        preflights += 1

    body = TinkerFin().run(source).astream().to_sse()

    await body.prepare(preflight=preflight)

    assert preflights == 1
    assert factory_calls == 0
    assert [frame async for frame in body] == []
    assert factory_calls == 1
