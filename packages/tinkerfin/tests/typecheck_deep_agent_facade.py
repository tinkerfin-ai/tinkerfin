"""Static inference checks for the generated public façade stubs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypedDict, assert_type, cast, reveal_type

from ag_ui.core import RunAgentInput
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from tinkerfin import (
    AgUiEventStream,
    DeepAgentAgUiRuntime,
    DeepAgentDefinition,
    DeepAgentRuntime,
    GraphRunStream,
    TinkerFin,
)


class _FakeModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _Context(TypedDict):
    tenant: str


if TYPE_CHECKING:
    tinkerfin = TinkerFin[str]()
    definition = tinkerfin.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
    )
    assert_type(definition, DeepAgentDefinition[_Context, str])

    run_input = RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    native = definition.new()
    agui = definition.new_agui(run_input=run_input)
    assert_type(native, DeepAgentRuntime[_Context])
    assert_type(agui, DeepAgentAgUiRuntime[_Context])

    graph_input = cast(InputAgentState, {"messages": []})
    assert_type(
        native.astream(graph_input, context={"tenant": "tenant-1"}),
        GraphRunStream[object],
    )
    assert_type(
        agui.astream(graph_input, context={"tenant": "tenant-1"}),
        AgUiEventStream,
    )
    reveal_type(tinkerfin.create_deep_agent)
    reveal_type(native.astream)
    reveal_type(agui.astream)
