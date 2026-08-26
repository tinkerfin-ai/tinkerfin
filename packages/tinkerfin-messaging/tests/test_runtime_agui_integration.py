"""Structural integration with request-scoped TinkerFin Deep Agent streams."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ag_ui.core import BaseEvent, RawEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.config import get_stream_writer

from tinkerfin import (
    DeepAgentDefinition,
    Identity,
    NativeGraphRunStream,
    TinkerFin,
)
from tinkerfin_messaging import (
    MessageSubscription,
    Messaging,
    NativeStreamPart,
)


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> Identity:
    return Identity(threadId=thread_id, runId=run_id)


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    """Keep the public fake model executable when Deep Agents binds tools."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


async def _native_parts(
    subscription: MessageSubscription[NativeStreamPart],
) -> list[NativeStreamPart]:
    return [message.data async for message in subscription]


async def _events(
    subscription: MessageSubscription[BaseEvent],
) -> list[BaseEvent]:
    return [message.data async for message in subscription]


def _definition(*, custom: bool = False) -> DeepAgentDefinition[None]:
    model = _ToolBindingFakeModel(
        responses=[
            *(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "emit_progress",
                                "args": {"value": 1},
                                "id": "call-progress",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
                if custom
                else []
            ),
            AIMessage(content="answer"),
        ]
    )
    return TinkerFin().create_deep_agent(
        model=model,
        tools=[_emit_progress] if custom else [],
        system_prompt="Answer briefly.",
    )


async def test_agui_stream_is_a_directly_iterable_message_source() -> None:
    events = (
        _definition()
        .new_agui(identity=_identity())
        .astream(InputAgentState(messages=[HumanMessage(content="AG-UI")]))
    )

    aiter(events)
    await events.aclose()


async def test_native_and_agui_streams_wrap_without_runtime_parameters() -> None:
    definition = _definition()

    async with Messaging() as messaging:
        native_channel = messaging.channel(name="native-events")
        native_identity = _identity(thread_id="native-thread", run_id="native-run")
        native_source = definition.new(identity=native_identity).astream(
            InputAgentState(messages=[HumanMessage(content="Native")]),
            stream_mode=("messages", "tasks", "values"),
            subgraphs=True,
        )
        assert isinstance(native_source, NativeGraphRunStream)
        assert native_source.messaging_identity is native_identity
        assert native_source.messaging_codec_profile == "langgraph.stream-part.v2.v1"
        assert native_source.messaging_source_type is Mapping
        assert native_source.messaging_replay_type is NativeStreamPart
        native = await native_channel.wrap(
            native_source,
            after=0,
        )

        agui_channel = messaging.channel(name="agui-events")
        agui_identity = _identity(thread_id="agui-thread")
        event_source = definition.new_agui(identity=agui_identity).astream(
            InputAgentState(messages=[HumanMessage(content="AG-UI")])
        )
        assert event_source.messaging_identity is agui_identity
        assert event_source.messaging_codec_profile == "agui.event.v1"
        agui = await agui_channel.wrap(
            event_source,
            after=0,
        )

        native_values = await _native_parts(native)
        agui_events = await _events(agui)

    assert native_values
    assert {part.mode for part in native_values} == {"messages", "tasks", "values"}
    assert agui_events[0].type == "RUN_STARTED"
    assert agui_events[-1].type == "RUN_FINISHED"


@tool("emit_progress")
def _emit_progress(value: int) -> str:
    """Emit one custom progress record for the active Deep Agent run."""

    get_stream_writer()({"progress": value})
    return f"emitted {value}"


async def test_real_custom_stream_is_consistent_across_all_consumers() -> None:
    def native_source(*, run_id: str):
        return (
            _definition(custom=True)
            .new(identity=_identity(thread_id="custom-thread", run_id=run_id))
            .astream(
                InputAgentState(messages=[HumanMessage(content="Report progress")]),
                stream_mode=("messages", "tasks", "values", "custom"),
                subgraphs=True,
            )
        )

    direct = [part async for part in native_source(run_id="custom-direct")]
    assert any(
        part["type"] == "custom" and part["data"] == {"progress": 1} for part in direct
    )

    frames = [frame async for frame in native_source(run_id="custom-sse").to_sse()]
    frame_payloads = [
        json.loads(frame.split("data: ", maxsplit=1)[1]) for frame in frames
    ]
    assert any(
        payload["type"] == "custom" and payload["data"] == {"progress": 1}
        for payload in frame_payloads
    )

    async with Messaging() as messaging:
        subscription = await messaging.channel(name="custom-native").wrap(
            native_source(run_id="custom-messaging"),
        )
        replay = await _native_parts(subscription)

    assert any(
        part.mode == "custom" and part.data == {"progress": 1} for part in replay
    )

    events = [
        event
        async for event in _definition(custom=True)
        .new_agui(identity=_identity(thread_id="custom-thread", run_id="custom-agui"))
        .astream(
            InputAgentState(messages=[HumanMessage(content="Report progress")]),
            stream_mode=("messages", "tasks", "values", "custom"),
        )
    ]
    raw_events = [
        event
        for event in events
        if isinstance(event, RawEvent) and event.source == "langgraph.custom"
    ]
    assert len(raw_events) == 1
    assert raw_events[0].source == "langgraph.custom"
    assert raw_events[0].event["data"] == {"progress": 1}
