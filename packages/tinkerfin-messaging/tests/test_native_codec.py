"""Optional LangGraph v2 native stream-part codec contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import get_type_hints

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.types import Interrupt
from pydantic import ValidationError

from tinkerfin import NativeStreamPart
from tinkerfin_messaging import NativeStreamPartCodec


def test_native_codec_declares_a_mapping_source_type() -> None:
    hints = get_type_hints(NativeStreamPartCodec.encode)

    assert hints["item"] == Mapping[str, object]


def test_native_codec_exposes_schema_id_on_the_codec_class() -> None:
    assert NativeStreamPartCodec.codec_id == "langgraph.stream-part.v2"


def test_native_codec_exposes_complete_live_and_replay_types() -> None:
    assert NativeStreamPartCodec.messaging_source_type is Mapping
    assert NativeStreamPartCodec.messaging_replay_type is NativeStreamPart


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
def test_native_codec_round_trips_every_supported_v2_mode(mode: str) -> None:
    codec = NativeStreamPartCodec()
    part: dict[str, object] = {
        "type": mode,
        "ns": ("child:task-1",),
        "data": {"mode": mode},
    }

    decoded = codec.decode(codec.encode(part))

    assert decoded.mode == mode
    assert decoded.namespace == ("child:task-1",)
    assert decoded.data == {"mode": mode}
    assert decoded.interrupts == ()


def test_native_codec_normalizes_messages_without_repr_serialization() -> None:
    codec = NativeStreamPartCodec()
    part = {
        "type": "messages",
        "ns": ("tools:task-1",),
        "data": (
            AIMessageChunk(id="message-1", content="hello"),
            {"langgraph_node": "model", "langgraph_step": 2},
        ),
    }

    decoded = codec.decode(codec.encode(part))

    assert codec.codec_id == "langgraph.stream-part.v2"
    assert decoded.mode == "messages"
    assert decoded.namespace == ("tools:task-1",)
    assert isinstance(decoded.data, dict)
    message_tuple = decoded.data
    assert message_tuple["$type"] == "tuple"
    items = message_tuple["items"]
    assert isinstance(items, list)
    message = items[0]
    assert isinstance(message, dict)
    assert message["$type"] == "langchain.message"
    assert "AIMessageChunk" in json.dumps(message, ensure_ascii=False)
    assert "repr" not in json.dumps(decoded.model_dump(mode="json"))


def test_native_codec_preserves_values_interrupts_and_business_state() -> None:
    codec = NativeStreamPartCodec()
    part = {
        "type": "values",
        "ns": (),
        "data": {
            "messages": [AIMessage(id="message-1", content="done")],
            "todos": [{"content": "keep", "status": "pending"}],
        },
        "interrupts": (
            Interrupt(value={"action_requests": [{"name": "write_file"}]}, id="i-1"),
        ),
    }

    decoded = codec.decode(codec.encode(part))

    assert decoded.mode == "values"
    assert decoded.namespace == ()
    assert isinstance(decoded.data, dict)
    assert decoded.data["todos"] == [{"content": "keep", "status": "pending"}]
    assert len(decoded.interrupts) == 1
    interrupt = decoded.interrupts[0]
    assert isinstance(interrupt, dict)
    assert interrupt == {
        "$type": "langgraph.interrupt",
        "id": "i-1",
        "value": {"action_requests": [{"name": "write_file"}]},
    }


def test_native_codec_normalizes_task_errors_without_loading_exception_classes() -> (
    None
):
    codec = NativeStreamPartCodec()
    part = {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": "task-1",
            "name": "model",
            "error": RuntimeError("provider failed"),
            "result": None,
            "interrupts": (),
        },
    }

    decoded = codec.decode(codec.encode(part))

    assert isinstance(decoded.data, dict)
    assert decoded.data["error"] == {
        "$type": "exception",
        "class": "builtins.RuntimeError",
        "message": "provider failed",
    }


@pytest.mark.parametrize(
    "part",
    [
        {"type": "unknown", "ns": (), "data": {}},
        {"type": "messages", "ns": "root", "data": {}},
        {"type": "values", "ns": (), "data": {"invalid": object()}},
        {"type": "values", "ns": (), "data": {"invalid": float("nan")}},
    ],
)
def test_native_codec_rejects_unsupported_or_non_finite_parts(
    part: dict[str, object],
) -> None:
    codec = NativeStreamPartCodec()

    with pytest.raises((TypeError, ValueError, ValidationError)):
        codec.encode(part)


def test_native_codec_rejects_list_namespaces() -> None:
    codec = NativeStreamPartCodec()

    with pytest.raises(TypeError, match="ns must be a tuple of strings"):
        codec.encode(
            {
                "type": "values",
                "ns": ["tools:task-1"],
                "data": {"answer": 42},
                "interrupts": (),
            }
        )


def test_native_sse_renderer_uses_sequence_and_current_json() -> None:
    codec = NativeStreamPartCodec()
    decoded = codec.decode(
        codec.encode({"type": "values", "ns": (), "data": {"answer": 42}})
    )

    frame = codec.render(seq=7, payload=decoded)

    assert frame.startswith(b"id: 7\nevent: stream-part\ndata: ")
    assert frame.endswith(b"\n\n")
    data = json.loads(frame.split(b"data: ", maxsplit=1)[1])
    assert data == {
        "type": "values",
        "ns": [],
        "data": {"answer": 42},
        "interrupts": [],
    }
