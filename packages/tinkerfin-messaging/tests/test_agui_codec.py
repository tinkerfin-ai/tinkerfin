"""Optional AG-UI payload and SSE rendering contracts."""

from __future__ import annotations

import json

import pytest
from ag_ui.core import BaseEvent, RunStartedEvent, TextMessageContentEvent
from pydantic import ValidationError

from tinkerfin_messaging import AgUiCodec


def test_agui_codec_exposes_schema_id_on_the_codec_class() -> None:
    assert AgUiCodec.codec_id == "agui.event"


def test_agui_codec_exposes_complete_live_and_replay_types() -> None:
    assert AgUiCodec.messaging_source_type is BaseEvent
    assert AgUiCodec.messaging_replay_type is BaseEvent


def test_agui_codec_round_trips_the_complete_event() -> None:
    codec = AgUiCodec()
    event = RunStartedEvent(
        thread_id="thread-1",
        run_id="run-1",
        parent_run_id="parent-1",
        raw_event={"source": "test"},
    )

    payload = codec.encode(event)
    decoded = codec.decode(payload)

    assert codec.codec_id == "agui.event"
    assert decoded == event
    assert json.loads(payload) == {
        "type": "RUN_STARTED",
        "rawEvent": {"source": "test"},
        "threadId": "thread-1",
        "runId": "run-1",
        "parentRunId": "parent-1",
    }


def test_agui_sse_renderer_uses_sequence_id_and_native_json_data() -> None:
    codec = AgUiCodec()
    event = TextMessageContentEvent(message_id="message-1", delta="hello")

    frame = codec.render(seq=42, payload=event)

    assert frame == (
        b'id: 42\ndata: {"type":"TEXT_MESSAGE_CONTENT",'
        b'"messageId":"message-1","delta":"hello"}\n\n'
    )


def test_agui_codec_rejects_an_unknown_event_type() -> None:
    codec = AgUiCodec()

    with pytest.raises(ValidationError):
        codec.decode(b'{"type":"NOT_AN_AGUI_EVENT"}')
