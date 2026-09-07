"""Attachment output types validate durable replay and snapshot wire contracts."""

import json
from pathlib import Path

import pytest
from ag_ui.core import MessagesSnapshotEvent, ToolCallResultEvent
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tinkerfin_agui_adapter import (
    AttachmentAssistantMessage,
    AttachmentMessagesSnapshotEvent,
    AttachmentToolCallResultEvent,
    AttachmentToolMessage,
    parse_attachment_output_event,
)
from tinkerfin_contracts.media import Attachment
from tinkerfin_messaging import AgUiCodec

_CONTRACTS = Path(__file__).parents[1] / "src/tinkerfin_agui_adapter/contracts"


@pytest.fixture
def attachment() -> Attachment:
    return Attachment(id="file", name="chart.png", mime_type="image/png", size_bytes=42)


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_typed_snapshot_validates_and_preserves_attachments(
    attachment: Attachment, role: str
):
    message = (
        AttachmentAssistantMessage(
            id="answer", content="chart", attachments=[attachment]
        )
        if role == "assistant"
        else AttachmentToolMessage(
            id="result", content="", tool_call_id="call", attachments=[attachment]
        )
    )
    event = AttachmentMessagesSnapshotEvent(messages=[message])
    wire = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire["messages"][0]["attachments"] == [attachment.model_dump(mode="json")]
    for mode in ("validation", "serialization"):
        Draft202012Validator(
            AttachmentMessagesSnapshotEvent.model_json_schema(mode=mode)
        ).validate(wire)
    replay = AgUiCodec().decode(AgUiCodec().encode(event))
    assert isinstance(replay, MessagesSnapshotEvent)
    restored = parse_attachment_output_event(replay)
    assert isinstance(restored, AttachmentMessagesSnapshotEvent)
    restored_message = restored.messages[0]
    assert isinstance(
        restored_message, AttachmentAssistantMessage | AttachmentToolMessage
    )
    assert restored_message.attachments == [attachment]
    assert restored.model_dump(mode="json", by_alias=True, exclude_none=True) == wire


def test_tool_result_round_trips_with_typed_replay(attachment: Attachment):
    event = AttachmentToolCallResultEvent(
        message_id="result",
        tool_call_id="call",
        content="done",
        role="tool",
        attachments=[attachment],
    )
    encoded = AgUiCodec().encode(event)
    restored = parse_attachment_output_event(AgUiCodec().decode(encoded))
    assert isinstance(restored, AttachmentToolCallResultEvent)
    assert restored.attachments == [attachment]
    assert restored == event
    assert isinstance(event, ToolCallResultEvent)


@pytest.mark.parametrize(
    "model, fields",
    [
        (
            AttachmentToolCallResultEvent,
            {"messageId": "result", "toolCallId": "call", "content": ""},
        ),
        (AttachmentAssistantMessage, {"id": "answer", "content": ""}),
        (AttachmentToolMessage, {"id": "result", "toolCallId": "call", "content": ""}),
    ],
)
@pytest.mark.parametrize(
    "bad",
    [
        None,
        "file",
        [{}],
        [{"id": "f", "name": "f", "mime_type": "image/png", "size_bytes": -1}],
    ],
)
def test_output_attachment_fields_reject_invalid_descriptors(model, fields, bad):
    with pytest.raises(ValidationError):
        model.model_validate({**fields, "attachments": bad})


def test_snapshot_replay_rejects_invalid_attachment_extras():
    event = MessagesSnapshotEvent.model_validate(
        {"messages": [{"id": "answer", "role": "assistant", "attachments": [{}]}]}
    )
    with pytest.raises(ValidationError):
        parse_attachment_output_event(event)


def test_generated_real_tool_fixture_matches_typed_output_schemas():
    fixture = json.loads((_CONTRACTS / "message-attachments.fixture.json").read_text())
    assert [event["type"] for event in fixture["events"]] == [
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "TEXT_MESSAGE_START",
        "CUSTOM",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]
    result = next(
        event for event in fixture["events"] if event["type"] == "TOOL_CALL_RESULT"
    )
    for event, schema_name in [
        (result, "tool-call-result"),
        (fixture["snapshots"][0], "messages-snapshot"),
    ]:
        schema = json.loads((_CONTRACTS / f"{schema_name}.schema.json").read_text())
        Draft202012Validator(schema).validate(event)
        typed = parse_attachment_output_event(event)
        replayed = parse_attachment_output_event(
            AgUiCodec().decode(AgUiCodec().encode(typed))
        )
        assert (
            replayed.model_dump(mode="json", by_alias=True, exclude_none=True) == event
        )
    assert (
        fixture["snapshots"][0]["messages"][0]["attachments"] == result["attachments"]
    )
    assert (
        fixture["snapshots"][0]["messages"][1]["attachments"]
        == fixture["events"][4]["value"]["attachments"]
    )
