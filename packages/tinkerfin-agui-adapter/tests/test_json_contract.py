"""Strict JSON semantics shared by every adapter protocol boundary."""

from __future__ import annotations

import math

import pytest
from ag_ui.core import (
    MessagesSnapshotEvent,
    RawEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    ToolCallResultEvent,
)
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from tinkerfin_agui_adapter import Identity
from tinkerfin_agui_adapter.adapter import DeepAgentAgUiAdapter
from tinkerfin_agui_adapter.reasoning import normalize_operational_data
from tinkerfin_agui_adapter.sse import encode_sse


def _adapter() -> DeepAgentAgUiAdapter:
    return DeepAgentAgUiAdapter(
        identity=Identity(threadId="thread-json", runId="run-json")
    )


@pytest.fixture(
    params=(float("nan"), float("inf"), float("-inf")),
    ids=("nan", "inf", "negative-inf"),
)
def non_finite(request: pytest.FixtureRequest) -> float:
    value = request.param
    assert isinstance(value, float) and not math.isfinite(value)
    return value


def test_operational_json_rejects_non_finite_floats_recursively(
    non_finite: float,
) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        normalize_operational_data({"nested": [{"value": non_finite}]})


def test_state_rejects_non_finite_floats_without_committing_previous_state(
    non_finite: float,
) -> None:
    adapter = _adapter()

    with pytest.raises(ValueError, match="non-finite"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"value": non_finite},
                "interrupts": (),
            }
        )

    retry = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"value": 1},
            "interrupts": (),
        }
    )
    snapshot = next(event for event in retry if isinstance(event, StateSnapshotEvent))
    assert snapshot.snapshot == {"value": 1}


def test_task_start_rejects_non_finite_input_and_allows_same_key_retry(
    non_finite: float,
) -> None:
    adapter = _adapter()

    def part(value: object) -> dict[str, object]:
        return {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-json",
                "name": "model",
                "input": {"value": value},
                "triggers": ("branch:to:model",),
            },
        }

    with pytest.raises(ValueError, match="non-finite"):
        adapter.process(part(non_finite))

    retry = adapter.process(part(1))
    assert [event.type.value for event in retry] == ["RAW"]


def test_complete_tool_args_reject_non_finite_floats_before_tool_state_changes(
    non_finite: float,
) -> None:
    adapter = _adapter()

    def part(value: object) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessage(
                    id="assistant-json-tool",
                    content="",
                    tool_calls=[
                        {
                            "name": "json_tool",
                            "args": {"value": value},
                            "id": "call-json-tool",
                            "type": "tool_call",
                        }
                    ],
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }

    with pytest.raises(ValueError, match="non-finite"):
        adapter.process(part(non_finite))

    assert [event.type.value for event in adapter.process(part(1))] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
    ]


def test_structured_tool_result_rejects_non_finite_floats_before_fingerprinting(
    non_finite: float,
) -> None:
    adapter = _adapter()
    adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-json-result",
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "json_tool",
                            "args": "{}",
                            "id": "call-json-result",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                    chunk_position="last",
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    def part(content: object) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage.model_validate(
                    {
                        "id": "result-json",
                        "name": "json_tool",
                        "tool_call_id": "call-json-result",
                        "content": content,
                    }
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }

    with pytest.raises(ValueError, match="non-finite"):
        adapter.process(part([{"type": "data", "payload": {"value": non_finite}}]))

    assert [event.type.value for event in adapter.process(part("valid"))] == [
        "TOOL_CALL_RESULT"
    ]


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": float("nan")}],
        [{"type": "text", "text": "visible", "metadata": {"value": float("inf")}}],
    ],
    ids=("text-value", "text-sibling"),
)
def test_structured_text_tool_result_validates_complete_json_before_projection(
    content: list[dict[str, object]],
) -> None:
    adapter = _adapter()

    def part(value: object) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage.model_validate(
                    {
                        "id": "result-structured-text",
                        "name": "json_tool",
                        "tool_call_id": "call-structured-text",
                        "content": value,
                    }
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }

    with pytest.raises(ValueError, match="non-finite"):
        adapter.process(part(content))

    assert [event.type.value for event in adapter.process(part("valid"))] == [
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
    ]


@pytest.mark.parametrize(
    "text_block",
    [
        {"type": "text"},
        {"type": "text", "text": None},
        {"type": "text", "text": 1},
        {"type": "text", "text": ["invalid"]},
        {"type": "text", "text": {"invalid": True}},
    ],
    ids=("missing", "none", "number", "list", "mapping"),
)
@pytest.mark.parametrize("mixed", [False, True], ids=("pure", "mixed"))
def test_tool_text_blocks_require_a_string_before_result_state_changes(
    text_block: dict[str, object],
    mixed: bool,
) -> None:
    adapter = _adapter()

    def part(content: object) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage.model_validate(
                    {
                        "id": "result-invalid-text",
                        "name": "content_tool",
                        "tool_call_id": "call-invalid-text",
                        "content": content,
                    }
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }

    invalid_content = (
        [{"type": "data", "payload": {"keep": True}}, text_block]
        if mixed
        else [text_block]
    )
    with pytest.raises(TypeError, match="text content blocks require a string"):
        adapter.process(part(invalid_content))

    retry = adapter.process(part([{"type": "text", "text": "valid"}]))
    assert [event.type.value for event in retry] == [
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
    ]
    result = retry[-1]
    assert isinstance(result, ToolCallResultEvent)
    assert result.content == "valid"


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text"}],
        [{"type": "text", "text": None}],
        [
            {"type": "data", "payload": {"keep": True}},
            {"type": "text", "text": 1},
        ],
    ],
    ids=("missing", "none", "mixed-number"),
)
def test_live_ai_text_blocks_fail_before_message_lifecycle_state_changes(
    content: list[str | dict[str, object]],
) -> None:
    adapter = _adapter()

    def part(value: list[str | dict[str, object]]) -> dict[str, object]:
        return {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id="assistant-invalid-text", content=value),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }

    with pytest.raises(TypeError, match="text content blocks require a string"):
        adapter.process(part(content))

    retry = adapter.process(part([{"type": "text", "text": "valid"}]))
    assert [event.type.value for event in retry] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
    ]
    text = retry[-1]
    assert isinstance(text, TextMessageContentEvent)
    assert text.delta == "valid"


def test_snapshot_text_blocks_fail_before_root_state_is_committed() -> None:
    adapter = _adapter()

    with pytest.raises(TypeError, match="text content blocks require a string"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {
                    "messages": [
                        AIMessage(
                            id="assistant-snapshot-text",
                            content=[{"type": "text", "text": None}],
                        )
                    ],
                    "state": "invalid",
                },
                "interrupts": ({"id": "pause", "value": {"pause": True}},),
            }
        )

    retry = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    AIMessage(
                        id="assistant-snapshot-text",
                        content=[{"type": "text", "text": "valid"}],
                    )
                ],
                "state": "valid",
            },
            "interrupts": ({"id": "pause", "value": {"pause": True}},),
        }
    )

    state = next(event for event in retry if isinstance(event, StateSnapshotEvent))
    messages = next(
        event for event in retry if isinstance(event, MessagesSnapshotEvent)
    )
    assert state.snapshot == {"state": "valid"}
    assert messages.messages[0].content == "valid"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain", "plain"),
        ([{"type": "text", "text": ""}], ""),
        ([{"type": "text", "text": "visible"}], "visible"),
        (
            [
                {"type": "text", "text": "visible"},
                {"type": "data", "payload": {"count": 1}},
            ],
            'visible{"type": "data", "payload": {"count": 1}}',
        ),
        (
            [{"type": "data", "payload": {"count": 1}}],
            '{"type": "data", "payload": {"count": 1}}',
        ),
    ],
    ids=("plain", "empty-text", "text", "mixed", "structured"),
)
def test_valid_tool_content_keeps_its_existing_projection(
    content: object,
    expected: str,
) -> None:
    adapter = _adapter()
    events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage.model_validate(
                    {
                        "id": "result-valid-content",
                        "name": "content_tool",
                        "tool_call_id": "call-valid-content",
                        "content": content,
                    }
                ),
                {"lc_agent_name": None, "langgraph_node": "tools"},
            ),
        }
    )

    result = events[-1]
    assert isinstance(result, ToolCallResultEvent)
    assert result.content == expected


def test_hitl_action_args_reject_non_finite_floats_without_state_mutation(
    non_finite: float,
) -> None:
    adapter = _adapter()

    with pytest.raises(ValueError, match="non-finite"):
        adapter.process(
            {
                "type": "values",
                "ns": (),
                "data": {"value": "uncommitted"},
                "interrupts": (
                    {
                        "id": "interrupt-json",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "json_tool",
                                    "args": {"value": non_finite},
                                }
                            ],
                            "review_configs": [
                                {
                                    "action_name": "json_tool",
                                    "allowed_decisions": ["approve"],
                                }
                            ],
                        },
                    },
                ),
            }
        )

    retry = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"value": "committed"},
            "interrupts": (),
        }
    )
    snapshot = next(event for event in retry if isinstance(event, StateSnapshotEvent))
    assert snapshot.snapshot == {"value": "committed"}


def test_sse_rejects_manually_constructed_non_finite_events(
    non_finite: float,
) -> None:
    event = RawEvent(source="test", event={"nested": {"value": non_finite}})

    with pytest.raises(ValueError, match="non-finite"):
        encode_sse(event)
