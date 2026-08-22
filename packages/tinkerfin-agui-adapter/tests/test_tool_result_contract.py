"""Versioned correlation for parent-completed child Tool results."""

from __future__ import annotations

import pytest
from ag_ui.core import (
    MessagesSnapshotEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ToolCallChunk,
    ToolMessage,
)
from pydantic import ValidationError

from tinkerfin_agui_adapter import (
    TOOL_RESULT_CORRELATION_KEY,
    TOOL_RESULT_CORRELATION_SCHEMA,
    AgUiStreamContractError,
    DeepAgentAgUiAdapter,
    Identity,
    ScopedIdCodec,
    ToolResultCorrelation,
)

_CHILD_NAMESPACE = ("execute_deep_agent:graph-task",)


def _adapter() -> DeepAgentAgUiAdapter:
    adapter = DeepAgentAgUiAdapter(
        identity=Identity(threadId="thread-1", runId="run-1")
    )
    adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "graph-task",
                "name": "execute_deep_agent",
                "input": {"messages": []},
                "triggers": ("branch:to:execute_deep_agent",),
            },
        }
    )
    return adapter


def _assistant() -> AIMessage:
    return AIMessage(
        id="assistant-todos",
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Work", "status": "in_progress"}]},
                "id": "todos-call",
                "type": "tool_call",
            }
        ],
    )


def _correlation() -> ToolResultCorrelation:
    codec = ScopedIdCodec()
    return ToolResultCorrelation(
        schema=TOOL_RESULT_CORRELATION_SCHEMA,
        toolCallId=codec.encode("tool", _CHILD_NAMESPACE, "todos-call"),
        parentMessageId=codec.encode("message", _CHILD_NAMESPACE, "assistant-todos"),
    )


def _result(correlation: ToolResultCorrelation) -> ToolMessage:
    return ToolMessage(
        "Updated todo list",
        id="todos-result",
        tool_call_id="todos-call",
        name="write_todos",
        additional_kwargs={
            TOOL_RESULT_CORRELATION_KEY: correlation.model_dump(
                mode="json",
                by_alias=True,
            )
        },
    )


def _open_todo_call(adapter: DeepAgentAgUiAdapter) -> ToolCallStartEvent:
    events = adapter.process(
        {
            "type": "messages",
            "ns": _CHILD_NAMESPACE,
            "data": (
                AIMessageChunk(
                    id="assistant-todos",
                    content="",
                    tool_call_chunks=[
                        ToolCallChunk(
                            name="write_todos",
                            args='{"todos":[{"content":"Work","status":"in_progress"}]}',
                            id="todos-call",
                            index=0,
                        )
                    ],
                    chunk_position="last",
                ),
                {"langgraph_node": "model"},
            ),
        }
    )
    return next(event for event in events if isinstance(event, ToolCallStartEvent))


def test_correlated_parent_result_reuses_live_and_snapshot_scoped_ids() -> None:
    adapter = _adapter()
    start = _open_todo_call(adapter)
    correlation = _correlation()

    result_part = {
        "type": "messages",
        "ns": (),
        "data": (_result(correlation), {"langgraph_node": "continue_deep_agent"}),
    }
    result_events = adapter.process(result_part)
    results = [
        event for event in result_events if isinstance(event, ToolCallResultEvent)
    ]
    assert len(results) == 1
    assert results[0].tool_call_id == start.tool_call_id == correlation.tool_call_id
    assert not any(isinstance(event, ToolCallStartEvent) for event in result_events)
    assert adapter.process(result_part) == []

    synchronized = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [_assistant(), _result(correlation)]},
            "interrupts": (
                {
                    "id": "pause",
                    "value": {
                        "schema": "tinkerfin.runtime-interrupt.v1",
                        "kind": "pause",
                        "responseSchema": {"type": "object"},
                        "metadata": {},
                    },
                },
            ),
        }
    )
    snapshot = next(
        event for event in synchronized if isinstance(event, MessagesSnapshotEvent)
    )
    assistant = snapshot.messages[0]
    tool_result = snapshot.messages[1]
    assert assistant.id == correlation.parent_message_id
    assert assistant.tool_calls is not None
    assert assistant.tool_calls[0].id == correlation.tool_call_id
    assert tool_result.tool_call_id == correlation.tool_call_id


def test_invalid_declared_correlation_fails_before_result_state_changes() -> None:
    adapter = _adapter()
    _open_todo_call(adapter)
    invalid = _correlation().model_copy(
        update={
            "tool_call_id": ScopedIdCodec().encode(
                "tool", _CHILD_NAMESPACE, "other-call"
            )
        }
    )

    with pytest.raises(AgUiStreamContractError):
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (_result(invalid), {"langgraph_node": "continue_deep_agent"}),
            }
        )

    retry = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                _result(_correlation()),
                {"langgraph_node": "continue_deep_agent"},
            ),
        }
    )
    assert sum(isinstance(event, ToolCallResultEvent) for event in retry) == 1


def test_tool_result_correlation_is_frozen_and_forbids_unknown_fields() -> None:
    correlation = _correlation()
    with pytest.raises(ValidationError):
        setattr(correlation, "tool_call_id", "other")
    with pytest.raises(ValidationError):
        ToolResultCorrelation.model_validate(
            {
                **correlation.model_dump(mode="json", by_alias=True),
                "unknown": True,
            }
        )
