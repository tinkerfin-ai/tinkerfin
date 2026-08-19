from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from ag_ui.core import RunAgentInput, RunFinishedEvent, RunStartedEvent
from langchain_core.messages import AIMessageChunk

from tinkerfin_agui_adapter import astream_events


def _run_input(*, parent_run_id: str | None = None) -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "parentRunId": parent_run_id,
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )


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
            run_input=_run_input(parent_run_id="parent-1"),
        )
    ]

    started = events[0]
    finished = events[-1]
    assert isinstance(started, RunStartedEvent)
    assert started.thread_id == "thread-1"
    assert started.run_id == "run-1"
    assert started.parent_run_id == "parent-1"
    assert started.input == _run_input(parent_run_id="parent-1")
    assert isinstance(finished, RunFinishedEvent)
    assert finished.thread_id == "thread-1"
    assert finished.run_id == "run-1"
