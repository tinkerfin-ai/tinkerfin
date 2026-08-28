"""Static inference checks for the generated public façade stubs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    TypedDict,
    assert_type,
    cast,
    reveal_type,
)

from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from tinkerfin import (
    AgUiEventStream,
    AgUiResumeBinding,
    DeepAgentAgUiResumeRuntime,
    DeepAgentAgUiRuntime,
    DeepAgentDefinition,
    DeepAgentRuntime,
    NativeGraphRunStream,
    RunIdentity,
    TinkerFin,
)
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationModel,
    ClarificationOption,
    ClarificationQuestionBase,
    ClarificationResponseBase,
    ClarificationType,
    PlanReviewAction,
    SingleChoiceQuestion,
    clarification_type,
)
from tinkerfin_contracts import RuntimeObserver


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


class _QuestionAttributes(ClarificationModel):
    category: str


class _OptionAttributes(ClarificationModel):
    priority: int


class _Option(ClarificationOption[_OptionAttributes]):
    pass


class _Question(SingleChoiceQuestion[_QuestionAttributes, _Option]):
    pass


class _Form(ClarificationForm[_Question]):
    pass


class _RatingQuestion(ClarificationQuestionBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    maximum: int


class _RatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    rating: int


if TYPE_CHECKING:
    tinkerfin = TinkerFin()
    observer = cast(RuntimeObserver, object())
    observed = tinkerfin.observe(observer)
    assert_type(observed, TinkerFin)
    observed_definition = observed.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
    )
    assert_type(observed_definition, DeepAgentDefinition[_Context])
    definition = tinkerfin.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
    )
    assert_type(definition, DeepAgentDefinition[_Context])

    planned = tinkerfin.plan(enabled=True)
    assert_type(planned, TinkerFin)
    editable_planned = tinkerfin.plan(
        allowed_review_actions=(PlanReviewAction.APPROVE, PlanReviewAction.EDIT)
    )
    assert_type(editable_planned, TinkerFin)
    planned_definition = planned.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
        context_schema=_Context,
        checkpointer=InMemorySaver(),
    )
    assert_type(planned_definition, DeepAgentDefinition[_Context])

    custom_planned = tinkerfin.plan(clarification_schema=_Form)
    assert_type(custom_planned, TinkerFin)
    custom_option = _Option(id="option", label="Option", attributes=None)
    assert_type(custom_option.attributes, _OptionAttributes | None)
    rating_type = clarification_type(
        type_id="acme:rating",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        normalize=lambda _question, response: {"rating": response.rating},
    )
    assert_type(
        rating_type,
        ClarificationType[_RatingQuestion, _RatingResponse],
    )
    assert_type(tinkerfin.plan(clarification_types=(rating_type,)), TinkerFin)

    identity = RunIdentity(threadId="thread-1", runId="run-1")
    native = definition.new(identity=identity)
    agui = definition.new_agui(identity=identity)
    resumed_agui = definition.new_agui(
        identity=identity,
        resume=AgUiResumeBinding(
            mode="resume",
            resume_data={"decisions": [{"type": "approve"}]},
            native_interrupt_ids=("interrupt-1",),
        ),
    )
    planned_native = planned_definition.new(identity=identity, mode="plan")
    planned_agui = planned_definition.new_agui(
        identity=identity,
        mode="default",
    )
    assert_type(native, DeepAgentRuntime[_Context])
    assert_type(agui, DeepAgentAgUiRuntime[_Context])
    assert_type(resumed_agui, DeepAgentAgUiResumeRuntime[_Context])
    assert_type(planned_native, DeepAgentRuntime[_Context])
    assert_type(planned_agui, DeepAgentAgUiRuntime[_Context])

    graph_input = cast(InputAgentState, {"messages": []})
    assert_type(
        native.astream(graph_input, context={"tenant": "tenant-1"}),
        NativeGraphRunStream,
    )
    assert_type(
        agui.astream(graph_input, context={"tenant": "tenant-1"}),
        AgUiEventStream,
    )
    assert_type(
        resumed_agui.astream(context={"tenant": "tenant-1"}),
        AgUiEventStream,
    )
    reveal_type(tinkerfin.create_deep_agent)
    reveal_type(native.astream)
    reveal_type(agui.astream)
    reveal_type(resumed_agui.astream)
