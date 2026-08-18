from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import pytest
from ag_ui.core import BaseEvent

from tinkerfin import TinkerFin


@pytest.mark.asyncio
async def test_run_binds_one_lazy_zero_argument_source_factory() -> None:
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

    run = TinkerFin().run(source, on_part=on_part)
    stream = run.astream()

    assert calls == 0
    assert await anext(stream) is part
    assert calls == 1
    assert observed == [part]

    await stream.aclose()

    assert closed is True


def test_run_allows_exactly_one_object_stream_claim() -> None:
    async def source() -> AsyncIterator[object]:
        if False:  # pragma: no cover - provides the source shape only
            yield None

    run = TinkerFin().run(source)

    run.astream()

    with pytest.raises(RuntimeError, match="one object stream"):
        run.astream()


@pytest.mark.asyncio
async def test_astream_agui_converts_the_bound_factory_without_a_parts_argument() -> (
    None
):
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

    events = (
        TinkerFin()
        .run(source)
        .astream_agui(
            thread_id="thread-1",
            run_id="run-1",
            on_event=on_event,
        )
    )
    delivered = [event async for event in events]

    assert [event.type.value for event in delivered] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert observed == delivered


@pytest.mark.asyncio
async def test_run_rejects_a_factory_result_without_async_iteration() -> None:
    def source() -> AsyncIterator[object]:
        return cast(AsyncIterator[object], object())

    stream = TinkerFin().run(source).astream()

    with pytest.raises(
        TypeError,
        match="source_factory must return an async iterator",
    ):
        await anext(stream)
