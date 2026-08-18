"""模型思考内容不得进入 AG-UI 事件的隐私契约"""

from __future__ import annotations

import json
from typing import Literal

from ag_ui.core import (
    BaseEvent,
    ReasoningMessageContentEvent,
    TextMessageContentEvent,
    ToolCallArgsEvent,
)
from langchain_core.messages import AIMessageChunk

from tinkerfin_agui_adapter.adapter import DeepAgentAgUiAdapter

_MESSAGE_ID = "lc_run--019fcd68-f466-4abc-9def-0123456789ab"


def _adapter(*, expose_reasoning_events: bool = False) -> DeepAgentAgUiAdapter:
    return DeepAgentAgUiAdapter(
        "run-1",
        expose_reasoning_events=expose_reasoning_events,
    )


def _process(chunk: AIMessageChunk) -> list[BaseEvent]:
    return _adapter().process(
        {
            "type": "messages",
            "ns": (),
            "data": (chunk, {"lc_agent_name": None, "langgraph_node": "model"}),
        }
    )


def test_reasoning_events_are_disabled_by_default() -> None:
    events = _process(
        AIMessageChunk(
            id=_MESSAGE_ID,
            content="",
            additional_kwargs={"reasoning_content": "不应下发的思考"},
        )
    )

    assert events == []


def test_content_blocks_are_not_provider_reasoning_and_text_remains_visible() -> None:
    adapter = _adapter(expose_reasoning_events=True)

    events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id=_MESSAGE_ID,
                    content=[
                        {"type": "reasoning", "reasoning": "业务推理字段"},
                        {"type": "text", "text": "可见答案"},
                    ],
                    response_metadata={"output_version": "v1"},
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    assert not any(event.type.value.startswith("REASONING_") for event in events)
    assert [event.type.value for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
    ]
    content = events[-1]
    assert isinstance(content, TextMessageContentEvent)
    assert content.delta == json.dumps(
        [
            {"type": "reasoning", "reasoning": "业务推理字段"},
            {"type": "text", "text": "可见答案"},
        ],
        ensure_ascii=False,
    )


def test_provider_reasoning_source_is_additional_kwargs() -> None:
    events = _adapter(expose_reasoning_events=True).process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id=_MESSAGE_ID,
                    content="",
                    additional_kwargs={"reasoning_content": "可公开的推理"},
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )

    assert [event.type.value for event in events] == [
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
    ]
    content = events[-1]
    assert isinstance(content, ReasoningMessageContentEvent)
    assert content.delta == "可公开的推理"


def test_reasoning_events_require_explicit_opt_in_and_close_before_visible_text() -> (
    None
):
    adapter = _adapter(expose_reasoning_events=True)

    events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id=_MESSAGE_ID,
                    content="",
                    additional_kwargs={"reasoning_content": "可公开的推理"},
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )
    events.extend(
        adapter.process(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessageChunk(
                        id=_MESSAGE_ID,
                        content="最终答案",
                        chunk_position="last",
                    ),
                    {"lc_agent_name": None, "langgraph_node": "model"},
                ),
            }
        )
    )

    assert [event.type.value for event in events] == [
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_END",
        "REASONING_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]


def _stream_tool_arguments(*, expose_reasoning_events: bool) -> list[BaseEvent]:
    adapter = _adapter(expose_reasoning_events=expose_reasoning_events)
    events: list[BaseEvent] = []
    fragments: tuple[tuple[str, Literal["last"] | None], ...] = (
        ('{"query":"deep', None),
        (' agents"}', "last"),
    )
    for args, chunk_position in fragments:
        events.extend(
            adapter.process(
                {
                    "type": "messages",
                    "ns": (),
                    "data": (
                        AIMessageChunk(
                            id=_MESSAGE_ID,
                            content="",
                            tool_call_chunks=[
                                {
                                    "name": "search"
                                    if chunk_position is None
                                    else None,
                                    "args": args,
                                    "id": (
                                        "call-search"
                                        if chunk_position is None
                                        else None
                                    ),
                                    "index": 0,
                                    "type": "tool_call_chunk",
                                }
                            ],
                            chunk_position=chunk_position,
                        ),
                        {"lc_agent_name": None, "langgraph_node": "model"},
                    ),
                }
            )
        )
    return events


def test_reasoning_policy_does_not_change_incremental_tool_arguments() -> None:
    hidden_events = _stream_tool_arguments(expose_reasoning_events=False)
    exposed_events = _stream_tool_arguments(expose_reasoning_events=True)

    assert [event.type.value for event in hidden_events] == [
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
    ]
    argument_events = [
        event for event in hidden_events if isinstance(event, ToolCallArgsEvent)
    ]
    assert [event.delta for event in argument_events] == [
        '{"query":"deep',
        ' agents"}',
    ]
    assert [
        event.model_dump(mode="json", by_alias=True, exclude_none=True)
        for event in hidden_events
    ] == [
        event.model_dump(mode="json", by_alias=True, exclude_none=True)
        for event in exposed_events
    ]
