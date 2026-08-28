"""Current native stream validation contracts."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.types import Interrupt

from tinkerfin_native_stream import (
    NativeMessageStreamPart,
    NativeStreamContractError,
    NativeValuesStreamPart,
    RuntimeInterruptEnvelope,
    validate_native_stream_part,
)


def test_runtime_interrupt_envelope_is_protocol_neutral_and_deterministic() -> None:
    envelope = RuntimeInterruptEnvelope(
        kind="tinkerfin:plan_review",
        message="Review the Plan",
        response_schema={"type": "object"},
        metadata={"revision": 1},
    )

    assert envelope.model_dump(mode="json", by_alias=True) == {
        "schema": "tinkerfin.runtime-interrupt",
        "kind": "tinkerfin:plan_review",
        "message": "Review the Plan",
        "responseSchema": {"type": "object"},
        "metadata": {"revision": 1},
    }


def test_message_and_values_parts_preserve_live_objects_and_namespaces() -> None:
    message = AIMessageChunk(id="message-1", content="visible")
    validated_message = validate_native_stream_part(
        {
            "type": "messages",
            "ns": ("tools:task-1",),
            "data": (message, {"langgraph_node": "model"}),
        }
    )
    validated_values = validate_native_stream_part(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [message], "todos": []},
            "interrupts": (Interrupt(value={"kind": "review"}, id="interrupt-1"),),
        }
    )

    assert isinstance(validated_message, NativeMessageStreamPart)
    assert validated_message.ns == ("tools:task-1",)
    assert validated_message.data.message is message
    assert isinstance(validated_values, NativeValuesStreamPart)
    assert validated_values.interrupts[0].id == "interrupt-1"


@pytest.mark.parametrize(
    "part",
    (
        {"type": "messages", "ns": (), "data": (AIMessageChunk(content="x"),)},
        {"type": "values", "ns": [], "data": {}},
        {"type": "tasks", "ns": (), "data": {"id": "task-1"}},
        {"type": "unknown", "ns": (), "data": {}},
    ),
)
def test_malformed_parts_fail_with_safe_context(part: object) -> None:
    with pytest.raises(NativeStreamContractError) as captured:
        validate_native_stream_part(part)

    assert captured.value.code.value == "native.stream_contract"
    assert "input" not in captured.value.diagnostic_context


def test_updates_require_non_empty_text_node_names() -> None:
    with pytest.raises(NativeStreamContractError):
        validate_native_stream_part(
            {"type": "updates", "ns": (), "data": {"": {"value": 1}}}
        )
