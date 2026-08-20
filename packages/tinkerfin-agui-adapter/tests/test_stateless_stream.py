from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from ag_ui.core import RunFinishedEvent, RunStartedEvent
from langchain_core.messages import AIMessageChunk

from tinkerfin_agui_adapter import Identity, astream_events


def _identity() -> Identity:
    return Identity(threadId="thread-1", runId="run-1")


@pytest.mark.asyncio
async def test_stream_converts_existing_parts_from_explicit_run_identity() -> None:
    async def parts() -> AsyncIterator[object]:
        yield {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="message-1",
                    content="hello",
                    chunk_position="last",
                ),
                {"langgraph_node": "model"},
            ),
        }
        yield {
            "type": "values",
            "ns": (),
            "data": {"messages": []},
            "interrupts": (),
        }

    events = [
        event
        async for event in astream_events(
            parts(),
            identity=_identity(),
        )
    ]

    started = events[0]
    finished = events[-1]
    assert isinstance(started, RunStartedEvent)
    assert started.thread_id == "thread-1"
    assert started.run_id == "run-1"
    assert started.parent_run_id is None
    assert started.input is None
    assert isinstance(finished, RunFinishedEvent)
    assert finished.thread_id == "thread-1"
    assert finished.run_id == "run-1"
