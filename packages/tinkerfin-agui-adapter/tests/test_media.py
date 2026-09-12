"""Durable attachments keep typed provenance across Tool replay and snapshots."""

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from tinkerfin_agui_adapter import DeepAgentAgUiAdapter, RunIdentity
from tinkerfin_contracts.media import Attachment


def test_tool_attachments_are_structured_and_replay_conflicts_are_rejected():
    attachment = Attachment(
        id="image-1", name="chart.png", mime_type="image/png", size_bytes=42
    )
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run")
    )
    message = ToolMessage(
        id="result",
        tool_call_id="call",
        name="chart",
        content=[attachment.content_block()],
    )
    part = {
        "type": "messages",
        "ns": (),
        "data": (message, {"lc_agent_name": None, "langgraph_node": "tools"}),
    }
    events = adapter.process(part)
    result = next(event for event in events if event.type == "TOOL_CALL_RESULT")
    wire = result.model_dump(mode="json", by_alias=True)
    assert wire["attachments"] == [attachment.model_dump(mode="json")]
    assert wire["content"] == ""
    assert adapter.process(part) == []
    changed = message.model_copy(
        update={
            "content": [attachment.model_copy(update={"id": "image-2"}).content_block()]
        }
    )
    with pytest.raises(ValueError):
        adapter.process({**part, "data": (changed, part["data"][1])})


def test_user_attachment_snapshot_preserves_reference_and_text():
    attachment = Attachment(
        id="file-1", name="report.pdf", mime_type="application/pdf", size_bytes=100
    )
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run")
    )
    message = HumanMessage(
        id="user",
        content=[{"type": "text", "text": "read"}, attachment.content_block()],
    )
    events = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"messages": [message]},
            "interrupts": ({"id": "pause", "value": {"pause": "review"}},),
        }
    )
    events += adapter.finish()
    snapshots = [event for event in events if event.type == "MESSAGES_SNAPSHOT"]
    assert snapshots
    content = snapshots[-1].model_dump(mode="json", by_alias=True)["messages"][0][
        "content"
    ]
    assert content[0]["text"] == "read"
    assert content[1]["source"]["value"] == "attachment:file-1"


def test_assistant_attachment_event_has_a_balanced_message_lifecycle():
    """An attachment-only assistant reply remains visible without a text delta."""
    from langchain_core.messages import AIMessageChunk

    attachment = Attachment(
        id="image", name="image.png", mime_type="image/png", size_bytes=10
    )
    adapter = DeepAgentAgUiAdapter(
        identity=RunIdentity(namespace="test", thread_id="thread", run_id="run")
    )
    events = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="answer",
                    content=[attachment.content_block()],
                    chunk_position="last",
                ),
                {"lc_agent_name": None, "langgraph_node": "model"},
            ),
        }
    )
    assert [event.type.value for event in events] == [
        "TEXT_MESSAGE_START",
        "CUSTOM",
        "TEXT_MESSAGE_END",
    ]
    assert events[1].model_dump(mode="json")["value"]["attachments"][0]["id"] == "image"
