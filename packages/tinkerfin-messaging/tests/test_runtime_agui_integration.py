"""Structural integration with stateless TinkerFin Graph streams."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, TypedDict

from ag_ui.core import BaseEvent, RawEvent
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from tinkerfin import AgUiNativeStreamConfig, TinkerFin
from tinkerfin_messaging import (
    MessageSubscription,
    Messaging,
    NativeStreamPart,
)


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


def _graph():
    model = _ToolBindingFakeModel(
        responses=[
            AIMessage(content="native answer"),
            AIMessage(content="agui answer"),
        ]
    )
    graph = create_deep_agent(
        model=model,
        system_prompt="Answer briefly.",
    )
    return graph


async def _empty_parts() -> AsyncIterator[object]:
    if False:  # pragma: no cover - defines the async iterator shape
        yield None


async def test_agui_stream_is_a_directly_iterable_message_source() -> None:
    events = (
        TinkerFin()
        .run(_empty_parts)
        .astream_agui(
            thread_id="thread-1",
            run_id="run-1",
        )
    )

    aiter(events)
    await events.aclose()


async def test_native_and_agui_streams_wrap_without_runtime_parameters() -> None:
    graph = _graph()

    async with Messaging() as messaging:
        native_channel = messaging.channel(name="native-events")
        native_invocation = AgUiNativeStreamConfig().bind(
            graph.astream,
            {"messages": [{"role": "user", "content": "Native"}]},
            config={"configurable": {"thread_id": "native-thread"}},
        )
        native_source = TinkerFin().run(native_invocation).astream()
        native = await native_channel.wrap(
            native_source,
            stream="native-conversation",
            run="native-run",
            after=0,
            attach_identity={
                "input": "Native",
                "streamMode": ["messages", "tasks", "values"],
            },
        )

        agui_channel = messaging.channel(name="agui-events")
        event_source = (
            TinkerFin()
            .run(
                lambda: graph.astream(
                    {"messages": [{"role": "user", "content": "AG-UI"}]},
                    config={"configurable": {"thread_id": "agui-thread"}},
                    stream_mode=("messages", "tasks", "values"),
                    version="v2",
                    subgraphs=True,
                )
            )
            .astream_agui(
                thread_id="agui-thread",
                run_id="run-1",
            )
        )
        agui = await agui_channel.wrap(
            event_source,
            stream="agui-conversation",
            run="run-1",
            after=0,
            attach_identity={"threadId": "agui-thread", "runId": "run-1"},
            cancel=event_source.abort,
        )

        native_values = await _native_parts(native)
        agui_events = await _events(agui)

    assert native_values
    assert {part.mode for part in native_values} == {"messages", "tasks", "values"}
    assert agui_events[0].type == "RUN_STARTED"
    assert agui_events[-1].type == "RUN_FINISHED"


class _CustomState(TypedDict):
    value: int


def _emit_custom(state: _CustomState) -> dict[str, int]:
    get_stream_writer()({"progress": state["value"]})
    return {"value": state["value"] + 1}


async def test_real_custom_stream_is_consistent_across_all_consumers() -> None:
    builder = StateGraph(_CustomState)
    builder.add_node("emit_custom", _emit_custom)
    builder.add_edge(START, "emit_custom")
    builder.add_edge("emit_custom", END)
    graph = builder.compile()

    def strict_run():
        invocation = AgUiNativeStreamConfig(extra_modes=("custom",)).bind(
            graph.astream,
            {"value": 1},
        )
        return TinkerFin().run(invocation)

    direct = [part async for part in strict_run().astream()]
    assert any(
        part["type"] == "custom" and part["data"] == {"progress": 1} for part in direct
    )

    frames = [frame async for frame in strict_run().astream().to_sse()]
    frame_payloads = [
        json.loads(frame.split("data: ", maxsplit=1)[1]) for frame in frames
    ]
    assert any(
        payload["type"] == "custom" and payload["data"] == {"progress": 1}
        for payload in frame_payloads
    )

    async with Messaging() as messaging:
        subscription = await messaging.channel(name="custom-native").wrap(
            strict_run().astream(),
            stream="custom-stream",
            run="custom-run",
        )
        replay = await _native_parts(subscription)

    assert any(
        part.mode == "custom" and part.data == {"progress": 1} for part in replay
    )

    events = [
        event
        async for event in strict_run().astream_agui(
            thread_id="custom-thread",
            run_id="custom-run",
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
