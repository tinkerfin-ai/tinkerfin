"""Optional AG-UI payload and SSE rendering contracts."""

from __future__ import annotations

import json

import pytest
from ag_ui.core import (
    BaseEvent,
    Context,
    Interrupt,
    MessagesSnapshotEvent,
    ResumeEntry,
    RunAgentInput,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
    Tool,
    ToolCallStartEvent,
    ToolMessage,
    UserMessage,
)
from pydantic import ValidationError

from tinkerfin_messaging import AgUiCodec


def _run_agent_input() -> RunAgentInput:
    return RunAgentInput(
        thread_id="thread-1",
        run_id="run-1",
        parent_run_id="parent-1",
        state={"status": "ready"},
        messages=[UserMessage(id="user-1", content="hello")],
        tools=[
            Tool(
                name="search",
                description="Search records",
                parameters={"type": "object"},
            )
        ],
        context=[Context(description="tenant", value="tenant-1")],
        forwarded_props={"surface": "studio"},
        resume=[
            ResumeEntry(
                interrupt_id="interrupt-1",
                status="resolved",
                payload={"type": "approve"},
            )
        ],
    )


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


@pytest.mark.parametrize(
    "event",
    (
        RunStartedEvent(
            thread_id="thread-authoritative",
            run_id="run-authoritative",
        ).model_copy(update={"threadId": "thread-extra", "runId": "run-extra"}),
        TextMessageStartEvent(
            message_id="message-authoritative",
            role="assistant",
        ).model_copy(update={"messageId": "message-extra"}),
        ToolCallStartEvent(
            tool_call_id="tool-authoritative",
            tool_call_name="search",
        ).model_copy(update={"toolCallId": "tool-extra"}),
        MessagesSnapshotEvent(
            messages=[
                ToolMessage(
                    id="result-1",
                    content="done",
                    tool_call_id="tool-authoritative",
                ).model_copy(update={"toolCallId": "tool-extra"})
            ]
        ),
        RunFinishedEvent(
            thread_id="thread-1",
            run_id="run-1",
            outcome=RunFinishedInterruptOutcome(
                interrupts=[
                    Interrupt(
                        id="interrupt-1",
                        reason="tool_call",
                        tool_call_id="tool-1",
                        response_schema={"type": "approve"},
                        expires_at="2026-09-01T00:00:00Z",
                    ).model_copy(
                        update={
                            "responseSchema": {"type": "reject"},
                            "expiresAt": "1999-01-01T00:00:00Z",
                        }
                    )
                ]
            ),
        ),
    ),
)
def test_agui_codec_rejects_alias_extras_that_override_protocol_identity(
    event: BaseEvent,
) -> None:
    with pytest.raises(ValueError, match="protocol identity"):
        AgUiCodec().encode(event)


@pytest.mark.parametrize(
    "collision",
    (
        "identity",
        "parent",
        "forwarded_props",
        "message",
        "resume",
    ),
)
def test_agui_codec_rejects_alias_extras_inside_run_input(collision: str) -> None:
    run_input = _run_agent_input()
    if collision == "identity":
        run_input = run_input.model_copy(
            update={"threadId": "thread-other", "runId": "run-other"}
        )
    elif collision == "parent":
        run_input = run_input.model_copy(update={"parentRunId": "parent-other"})
    elif collision == "forwarded_props":
        run_input = run_input.model_copy(
            update={"forwardedProps": {"surface": "other"}}
        )
    elif collision == "message":
        message = run_input.messages[0].model_copy(
            update={"encryptedValue": "encrypted-other"}
        )
        run_input = run_input.model_copy(update={"messages": [message]})
    else:
        assert run_input.resume is not None
        resume = run_input.resume[0].model_copy(
            update={"interruptId": "interrupt-other"}
        )
        run_input = run_input.model_copy(update={"resume": [resume]})
    event = RunStartedEvent(
        thread_id="thread-1",
        run_id="run-1",
        parent_run_id="parent-1",
        input=run_input,
    )

    with pytest.raises(ValueError, match="protocol identity"):
        AgUiCodec().encode(event)


def test_agui_codec_rejects_run_input_from_another_start() -> None:
    run_input = _run_agent_input().model_copy(
        update={"thread_id": "thread-other", "run_id": "run-other"}
    )
    event = RunStartedEvent(
        thread_id="thread-1",
        run_id="run-1",
        parent_run_id="parent-1",
        input=run_input,
    )

    with pytest.raises(ValueError, match="RunIdentity input"):
        AgUiCodec().encode(event)


def test_agui_codec_preserves_non_protocol_product_extras() -> None:
    event = RunStartedEvent(thread_id="thread-1", run_id="run-1").model_copy(
        update={"title": "Product title"}
    )

    decoded = AgUiCodec().decode(AgUiCodec().encode(event))

    assert isinstance(decoded, RunStartedEvent)
    assert decoded.thread_id == "thread-1"
    assert decoded.run_id == "run-1"
    assert decoded.model_extra == {"title": "Product title"}


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


def test_agui_codec_preserves_declared_extension_fields_and_rejects_identity_changes() -> (
    None
):
    class LabeledTextStart(TextMessageStartEvent):
        label: str

    codec = AgUiCodec()
    event = LabeledTextStart(message_id="answer", role="assistant", label="Chart")
    replay = codec.decode(codec.encode(event))
    assert isinstance(replay, TextMessageStartEvent)
    assert replay.model_extra == {"label": "Chart"}
    with pytest.raises(ValueError, match="protocol identity"):
        codec.encode(event.model_copy(update={"messageId": "other"}))
    with pytest.raises((ValueError, TypeError)):
        codec.encode(event.model_copy(update={"type": "TEXT_MESSAGE_END"}))


def test_agui_codec_preserves_typed_fields_within_authoritative_run_input() -> None:
    class LabeledUserMessage(UserMessage):
        label: str

    run_input = _run_agent_input()
    run_input.messages = [
        LabeledUserMessage(id="user", content="hello", label="Image request")
    ]
    event = RunStartedEvent(
        thread_id="thread-1", run_id="run-1", parent_run_id="parent-1", input=run_input
    )
    replay = AgUiCodec().decode(AgUiCodec().encode(event))
    assert isinstance(replay, RunStartedEvent)
    assert replay.input is not None
    assert replay.input.messages[0].model_extra == {"label": "Image request"}
