"""Public Plan Mode contracts and native Deep Agent handoff behavior."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunFinishedEvent, StateSnapshotEvent
from ag_ui.core.types import ResumeEntry
from deepagents import create_deep_agent
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from deepagents.graph import DeepAgentState
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.tools import tool
from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Interrupt
from pydantic import PrivateAttr, ValidationError, field_serializer

import tinkerfin.plan as plan_api
from tinkerfin import AgUiResumeBinding, Identity, TinkerFin
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationFormBase,
    ClarificationModel,
    ClarificationOption,
    ClarificationQuestion,
    DefaultClarificationForm,
    PlanContent,
    PlanDraft,
    PlanModeConfigurationError,
    PlanState,
    PlanStatus,
    PlanStep,
    PlanStructuredOutputError,
)
from tinkerfin_agui_adapter import ResumeMapper, ScopedIdCodec


class _FakeModel(FakeMessagesListChatModel):
    _bound_tool_names: list[tuple[str, ...]] = PrivateAttr(default_factory=list)
    _model_inputs: list[tuple[BaseMessage, ...]] = PrivateAttr(default_factory=list)

    @property
    def bound_tool_names(self) -> tuple[tuple[str, ...], ...]:
        return tuple(self._bound_tool_names)

    @property
    def model_inputs(self) -> tuple[tuple[BaseMessage, ...], ...]:
        return tuple(self._model_inputs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._model_inputs.append(tuple(messages))
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tool_choice, kwargs
        self._bound_tool_names.append(
            tuple(
                tool.name if isinstance(tool, BaseTool) else str(tool) for tool in tools
            )
        )
        return self


class _GlobalState(DeepAgentState):
    global_marker: str


class _DefinitionState(DeepAgentState):
    definition_marker: str


class _RuntimeContext(TypedDict):
    tenant: str


class _ContextAwareModel(_FakeModel):
    _contexts: list[_RuntimeContext] = PrivateAttr(default_factory=list)

    @property
    def contexts(self) -> tuple[_RuntimeContext, ...]:
        return tuple(self._contexts)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        from langgraph.runtime import get_runtime

        context = get_runtime(_RuntimeContext).context
        assert context is not None
        self._contexts.append(context)
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


class _QuestionAttributes(ClarificationModel):
    category: str


class _OptionAttributes(ClarificationModel):
    priority: int


class _CustomOption(ClarificationOption[_OptionAttributes]):
    pass


class _CustomQuestion(ClarificationQuestion[_QuestionAttributes, _OptionAttributes]):
    options: tuple[_CustomOption, ...] = ()


class _CustomForm(ClarificationForm[_CustomQuestion]):
    pass


class _OpaqueQuestionAttributes(ClarificationModel):
    token: object

    @field_serializer("token")
    def token_is_not_json(self, value: object) -> object:
        del value
        return object()


class _OpaqueQuestion(
    ClarificationQuestion[_OpaqueQuestionAttributes, ClarificationModel]
):
    pass


class _OpaqueForm(ClarificationForm[_OpaqueQuestion]):
    pass


class _InvalidListForm(ClarificationFormBase):
    questions: list[ClarificationQuestion] = []


def _identity(run_id: str, *, thread_id: str = "plan-thread") -> Identity:
    return Identity(threadId=thread_id, runId=run_id)


def _planner(
    *,
    suffix: str = "",
    goal: str | None = None,
) -> AIMessage:
    resolved_goal = goal or f"Implement feature{suffix}"
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "clarification": None,
                    "draft": {
                        "goal": resolved_goal,
                        "assumptions": [],
                        "steps": [
                            {
                                "id": "step-1",
                                "title": "Implement",
                                "description": "Implement the requested feature",
                                "verification": ["Targeted tests pass"],
                            }
                        ],
                        "acceptance_criteria": ["Feature works"],
                    },
                },
                "id": f"planner{suffix or '-1'}",
                "type": "tool_call",
            }
        ],
        id=f"planner-message{suffix or '-1'}",
    )


def _planner_clarification(
    *,
    question_id: str = "target",
    prompt: str = "Which target?",
    options: list[dict[str, object]] | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "draft": None,
                    "clarification": {
                        "questions": [
                            {
                                "id": question_id,
                                "prompt": prompt,
                                "options": options or [],
                                "allow_free_text": True,
                            }
                        ]
                    },
                },
                "id": f"clarify-{question_id}",
                "type": "tool_call",
            }
        ],
        id=f"clarify-message-{question_id}",
    )


def _custom_planner_clarification() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "draft": None,
                    "clarification": {
                        "questions": [
                            {
                                "id": "custom-target",
                                "prompt": "Choose a target",
                                "allow_free_text": True,
                                "attributes": {"category": "deployment"},
                                "options": [
                                    {
                                        "id": "target-a",
                                        "label": "Target A",
                                        "description": "Use target A",
                                        "attributes": {"priority": 1},
                                    }
                                ],
                            }
                        ]
                    },
                },
                "id": "custom-clarification",
                "type": "tool_call",
            }
        ],
        id="custom-clarification-message",
    )


def _opaque_planner_clarification() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "draft": None,
                    "clarification": {
                        "questions": [
                            {
                                "id": "opaque",
                                "prompt": "Choose",
                                "attributes": {"token": "private"},
                            }
                        ]
                    },
                },
                "id": "opaque-clarification",
                "type": "tool_call",
            }
        ],
    )


def _accept_edit() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "accept_edit",
                    "clarification": None,
                    "draft": None,
                },
                "id": "accept-edit",
                "type": "tool_call",
            }
        ],
        id="accept-edit-message",
    )


def _invalid_planner_json() -> AIMessage:
    return AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": '{"type":"draft",}',
                "id": "invalid-planner-json",
                "error": "invalid JSON arguments",
                "type": "invalid_tool_call",
            }
        ],
        id="invalid-planner-message",
    )


def _todo_call(*, content: str = "Execute") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": content, "status": "in_progress"}]},
                "id": f"todo-{content}",
                "type": "tool_call",
            }
        ],
        id=f"todo-message-{content}",
    )


async def _parts(
    definition: object,
    graph_input: object,
    *,
    run_id: str,
    config: Mapping[str, object],
    mode: str | None = None,
    durability: str | None = None,
    context: object | None = None,
) -> list[Mapping[str, object]]:
    runtime = cast(Any, definition).new(identity=_identity(run_id), mode=mode)
    options: dict[str, object] = {
        "config": config,
        "stream_mode": ["messages", "tasks", "values"],
        "version": "v2",
        "subgraphs": True,
    }
    if durability is not None:
        options["durability"] = durability
    if context is not None:
        options["context"] = context
    return [part async for part in runtime.astream(graph_input, **options)]


async def _agui_events(
    definition: object,
    graph_input: object,
    *,
    run_id: str,
    config: Mapping[str, object],
    mode: str | None = None,
    resume: AgUiResumeBinding | None = None,
) -> list[BaseEvent]:
    runtime = cast(Any, definition).new_agui(
        identity=_identity(run_id),
        mode=mode,
        resume=resume,
    )
    return [
        event
        async for event in runtime.astream(
            graph_input,
            config=config,
        )
    ]


def _root_values(parts: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [
        cast(Mapping[str, object], part["data"])
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]


def _root_interrupts(parts: Sequence[Mapping[str, object]]) -> tuple[Interrupt, ...]:
    frames = [
        cast(tuple[Interrupt, ...], part.get("interrupts", ()))
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]
    return frames[-1]


def _terminal(events: Sequence[BaseEvent]) -> RunFinishedEvent:
    terminals = [event for event in events if isinstance(event, RunFinishedEvent)]
    assert len(terminals) == 1
    return terminals[0]


def _resume_entry(interrupt_id: str, payload: Mapping[str, object]) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": "resolved",
            "payload": dict(payload),
        }
    )


def _plan_binding(
    terminal: RunFinishedEvent,
    *,
    run_id: str,
    payload: Mapping[str, object],
) -> AgUiResumeBinding:
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    interrupt = terminal.outcome.interrupts[0]
    translation = ResumeMapper().map_agui(
        entries=(_resume_entry(interrupt.id, payload),),
        interrupts=terminal.outcome.interrupts,
    )
    return AgUiResumeBinding.from_translation(
        identity=_identity(run_id),
        translation=translation,
    )


def test_plan_configuration_is_immutable_and_preserves_upstream_signature() -> None:
    base = TinkerFin()
    planned = base.plan(enabled=True, default_mode="plan")

    assert planned is not base
    assert inspect.signature(planned.create_deep_agent) == inspect.signature(
        create_deep_agent
    )
    base.create_deep_agent(model=_FakeModel(responses=[AIMessage(content="ok")]))
    planned.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="default only")])
    )


def test_plan_package_exports_only_the_v3_contract() -> None:
    assert set(plan_api.__all__) == {
        "AgentMode",
        "ClarificationExchange",
        "ClarificationForm",
        "ClarificationFormBase",
        "ClarificationModel",
        "ClarificationOption",
        "ClarificationOptionBase",
        "ClarificationQuestion",
        "ClarificationQuestionBase",
        "ConfirmedPlan",
        "DefaultClarificationForm",
        "PendingClarification",
        "PlanContent",
        "PlanDraft",
        "PlanHandoff",
        "PlanModeConfigurationError",
        "PlanReviewAction",
        "PlanState",
        "PlanStatus",
        "PlanStep",
        "PlanStructuredOutputError",
        "RequirementAnswer",
    }


@pytest.mark.parametrize("value", [1, "true", None, object()])
def test_plan_configuration_requires_a_strict_bool(value: object) -> None:
    with pytest.raises(TypeError, match="enabled must be a bool"):
        TinkerFin().plan(enabled=cast(Any, value))


def test_plan_configuration_rejects_disabled_options_and_invalid_modes() -> None:
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().plan(enabled=False, default_mode="plan")
    with pytest.raises(PlanModeConfigurationError, match="default_mode"):
        TinkerFin().plan(default_mode=cast(Any, "automatic"))


@pytest.mark.parametrize(
    "schema",
    [ClarificationModel, ClarificationForm, _InvalidListForm],
)
def test_plan_configuration_rejects_invalid_clarification_schemas(
    schema: object,
) -> None:
    with pytest.raises(PlanModeConfigurationError):
        TinkerFin().plan(clarification_schema=cast(Any, schema))


def test_plan_contracts_are_frozen_and_reject_duplicate_steps() -> None:
    step = PlanStep(
        id="step-1",
        title="Implement",
        description="Implement feature",
        verification=("Tests pass",),
    )
    content = PlanContent(
        goal="Implement feature",
        steps=(step,),
        acceptance_criteria=("Feature works",),
    )
    draft = PlanDraft(
        revision=1,
        **content.model_dump(mode="python", by_alias=False),
    )
    with pytest.raises(ValidationError):
        draft.revision = 2  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError, match="step IDs must be unique"):
        PlanContent(
            goal="Implement",
            steps=(step, step),
            acceptance_criteria=("Works",),
        )
    assert PlanState().workflow_version == "tinkerfin.plan.v3"


def test_clarification_forms_limit_each_round_to_three_questions() -> None:
    with pytest.raises(ValidationError, match="at most"):
        DefaultClarificationForm.model_validate(
            {
                "questions": [
                    {"id": f"question-{index}", "prompt": "Choose"}
                    for index in range(4)
                ]
            }
        )


def test_plain_definition_rejects_plan_runs() -> None:
    definition = TinkerFin().create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="done")]),
        tools=[],
    )
    with pytest.raises(PlanModeConfigurationError, match="Plan-capable"):
        definition.new(identity=_identity("plain-plan"), mode="plan")


@pytest.mark.asyncio
async def test_plan_capable_default_is_native_and_needs_no_checkpointer() -> None:
    model = _FakeModel(responses=[AIMessage(content="done", id="native-result")])
    definition = TinkerFin().plan(enabled=True).create_deep_agent(model=model, tools=[])

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Run", id="default-message")]},
        run_id="default-no-saver",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )

    assert all("tinkerfin_plan" not in state for state in _root_values(parts))
    task_names = {
        cast(Mapping[str, object], part["data"]).get("name")
        for part in parts
        if part["type"] == "tasks"
    }
    assert "initialize_plan" not in task_names
    assert "create_plan" not in task_names
    assert all(not part["ns"] for part in parts)


@pytest.mark.asyncio
async def test_plan_run_requires_the_definition_checkpointer_only_when_selected() -> (
    None
):
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
        )
    )
    with pytest.raises(PlanModeConfigurationError, match="BaseCheckpointSaver"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Plan", id="plan-message")]},
            run_id="plan-no-saver",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
        )


@pytest.mark.asyncio
async def test_default_preserves_coordinator_and_composed_state() -> None:
    coordinated: list[Identity] = []

    @asynccontextmanager
    async def coordinate(identity: Identity) -> AsyncIterator[None]:
        coordinated.append(identity)
        yield

    definition = (
        TinkerFin(
            run_coordinator=coordinate,
            state_schema=_GlobalState,
        )
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[AIMessage(content="done")]),
            tools=[],
            state_schema=_DefinitionState,
        )
    )
    parts = await _parts(
        definition,
        {
            "messages": [HumanMessage(content="Run")],
            "global_marker": "global",
            "definition_marker": "definition",
        },
        run_id="composed-default",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )
    assert coordinated == [_identity("composed-default")]
    final = _root_values(parts)[-1]
    assert final["global_marker"] == "global"
    assert final["definition_marker"] == "definition"


@pytest.mark.asyncio
async def test_default_todos_use_the_native_root_without_correlation_extensions() -> (
    None
):
    model = _FakeModel(responses=[_todo_call(), AIMessage(content="done")])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            middleware=(TodoListMiddleware(),),
            checkpointer=InMemorySaver(),
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Run todos")]},
        run_id="native-todos",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )
    tool_messages = [
        cast(tuple[BaseMessage, object], part["data"])[0]
        for part in parts
        if part["type"] == "messages" and part["ns"] == ()
    ]
    todo_result = next(
        message
        for message in tool_messages
        if isinstance(message, ToolMessage) and message.tool_call_id == "todo-Execute"
    )
    assert "tinkerfinToolResult" not in todo_result.additional_kwargs
    assert _root_values(parts)[-1]["todos"] == [
        {"content": "Execute", "status": "in_progress"}
    ]


@pytest.mark.asyncio
async def test_planner_creates_review_without_a_deep_agent_parent_graph() -> None:
    saver = InMemorySaver()
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
            checkpointer=saver,
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="request-1")]},
        run_id="review",
        config={"configurable": {"thread_id": "plan-thread"}},
    )
    interrupt = _root_interrupts(parts)[0]
    plan = PlanState.model_validate(_root_values(parts)[-1]["tinkerfin_plan"])
    assert interrupt.value["kind"] == "plan_review"
    assert plan.status is PlanStatus.AWAITING_REVIEW
    assert plan.revision == 1
    assert plan.request_message_id == "request-1"
    task_names = {
        cast(Mapping[str, object], part["data"]).get("name")
        for part in parts
        if part["type"] == "tasks"
    }
    assert "execute_deep_agent" not in task_names
    namespaces = {
        str(checkpoint.config.get("configurable", {}).get("checkpoint_ns", ""))
        for checkpoint in saver.list({"configurable": {"thread_id": "plan-thread"}})
    }
    assert not any(
        namespace.startswith("execute_deep_agent:") for namespace in namespaces
    )


@pytest.mark.asyncio
async def test_planner_retries_one_provider_invalid_json_tool_call() -> None:
    model = _FakeModel(responses=[_invalid_planner_json(), _planner()])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="invalid-json-retry",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert _root_interrupts(parts)[0].value["kind"] == "plan_review"
    assert len(model.model_inputs) == 2
    assert any(
        isinstance(message, HumanMessage)
        and "strict JSON without trailing commas" in str(message.content)
        for message in model.model_inputs[-1]
    )


@pytest.mark.asyncio
async def test_plan_approval_hands_off_to_native_with_the_same_message_id() -> None:
    model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(
                serde=JsonPlusSerializer(allowed_msgpack_modules=None)
            ),
        )
    )
    config: RunnableConfig = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="request-1")]},
        run_id="approve-1",
        config=config,
        mode="plan",
    )
    completed = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="approve-2",
        config=config,
        mode="default",
    )
    final = _root_values(completed)[-1]
    plan = PlanState.model_validate(final["tinkerfin_plan"])
    assert plan.status is PlanStatus.APPROVED
    assert plan.effective_mode == "default"
    assert plan.handoff is not None
    final_messages = cast(Sequence[BaseMessage], final["messages"])
    assert final_messages[-1].content == "native done"
    execution_input = model.model_inputs[-1]
    handoff_message = next(
        message
        for message in execution_input
        if isinstance(message, HumanMessage) and message.id == "request-1"
    )
    assert "<tinkerfin-approved-plan" in str(handoff_message.content)
    assert plan.handoff.digest in str(handoff_message.content)
    assert not any(
        isinstance(message, SystemMessage)
        and "<tinkerfin-approved-plan" in str(message.content)
        for message in execution_input
    )
    assert all(
        "execute_deep_agent:" not in "|".join(cast(tuple[str, ...], part["ns"]))
        for part in completed
    )
    assert _root_interrupts(first)[0].value["kind"] == "plan_review"


@pytest.mark.asyncio
async def test_duplicate_plan_approval_does_not_execute_native_twice() -> None:
    model = _FakeModel(responses=[_planner(), AIMessage(content="done")])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="duplicate-1",
        config=config,
        mode="plan",
    )
    command = Command(resume={"type": "approve", "baseRevision": 1})
    await _parts(
        definition,
        command,
        run_id="duplicate-2",
        config=config,
        mode="default",
    )
    calls = len(model.model_inputs)
    duplicate = await _parts(
        definition,
        command,
        run_id="duplicate-3",
        config=config,
        mode="default",
    )
    assert len(model.model_inputs) == calls
    assert len(_root_values(duplicate)) == 1
    assert (
        PlanState.model_validate(_root_values(duplicate)[0]["tinkerfin_plan"]).status
        is PlanStatus.APPROVED
    )


@pytest.mark.asyncio
async def test_native_conversation_continues_after_plan_handoff() -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            AIMessage(content="first done", id="first-assistant"),
            AIMessage(content="second done", id="second-assistant"),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="First", id="first-user")]},
        run_id="continuity-1",
        config=config,
        mode="plan",
    )
    await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="continuity-2",
        config=config,
        mode="default",
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Second", id="second-user")]},
        run_id="continuity-3",
        config=config,
        mode="default",
    )
    final_input = model.model_inputs[-1]
    assert any(
        isinstance(message, AIMessage)
        and message.id == "first-assistant"
        and message.content == "first done"
        for message in final_input
    )
    assert any(
        isinstance(message, HumanMessage) and message.id == "second-user"
        for message in final_input
    )


@pytest.mark.asyncio
async def test_plan_default_plan_with_todos_regresses_historical_correlation_failure() -> (
    None
):
    model = _FakeModel(
        responses=[
            _planner(),
            _todo_call(content="First Plan"),
            AIMessage(content="first done"),
            AIMessage(content="default done"),
            _planner(suffix=" second"),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            middleware=(TodoListMiddleware(),),
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="First", id="first-message")]},
        run_id="first-review",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(review),
        run_id="first-execution",
        payload={"type": "approve", "baseRevision": 1},
    )
    executed = await _agui_events(
        definition,
        binding.command,
        run_id="first-execution",
        config=config,
        mode="default",
        resume=binding,
    )
    default = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Default", id="default-message")]},
        run_id="default-round",
        config=config,
        mode="default",
    )
    second = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Second", id="second-message")]},
        run_id="second-review",
        config=config,
        mode="plan",
    )
    executed_outcome = _terminal(executed).outcome
    default_outcome = _terminal(default).outcome
    second_outcome = _terminal(second).outcome
    assert executed_outcome is not None and executed_outcome.type == "success"
    assert default_outcome is not None and default_outcome.type == "success"
    assert second_outcome is not None and second_outcome.type == "interrupt"
    assert second_outcome.interrupts[0].reason == "plan_review"
    assert not any(isinstance(event, RunErrorEvent) for event in second)


@pytest.mark.asyncio
async def test_multiple_clarification_rounds_do_not_increment_revision() -> None:
    model = _FakeModel(
        responses=[
            _planner_clarification(
                question_id="environment",
                options=[{"id": "staging", "label": "Staging"}],
            ),
            _planner_clarification(question_id="region", prompt="Which region?"),
            _planner(),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="deploy-message")]},
        run_id="clarify-1",
        config=config,
        mode="plan",
    )
    assert (
        PlanState.model_validate(_root_values(first)[-1]["tinkerfin_plan"]).revision
        == 0
    )
    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [{"questionId": "environment", "optionId": "staging"}],
            }
        ),
        run_id="clarify-2",
        config=config,
        mode="plan",
    )
    plan2 = PlanState.model_validate(_root_values(second)[-1]["tinkerfin_plan"])
    assert plan2.revision == 0
    assert len(plan2.clarification_history) == 1
    third = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [{"questionId": "region", "answer": "EU"}],
            }
        ),
        run_id="clarify-3",
        config=config,
        mode="plan",
    )
    plan3 = PlanState.model_validate(_root_values(third)[-1]["tinkerfin_plan"])
    assert plan3.revision == 1
    assert len(plan3.clarification_history) == 2
    assert _root_interrupts(third)[0].value["kind"] == "plan_review"


@pytest.mark.asyncio
async def test_custom_clarification_preserves_attributes_and_trusted_option_label() -> (
    None
):
    model = _FakeModel(responses=[_custom_planner_clarification(), _planner()])
    definition = (
        TinkerFin()
        .plan(
            enabled=True,
            clarification_schema=_CustomForm,
        )
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False)),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="custom-message")]},
        run_id="custom-1",
        config=config,
        mode="plan",
    )
    interrupt = _root_interrupts(first)[0]
    metadata = interrupt.value["metadata"]
    form = metadata["clarification"]["form"]
    assert form["questions"][0]["attributes"] == {"category": "deployment"}
    assert form["questions"][0]["options"][0]["attributes"] == {"priority": 1}
    assert "schemaFingerprint" not in metadata["clarification"]
    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [{"questionId": "custom-target", "optionId": "target-a"}],
            }
        ),
        run_id="custom-2",
        config=config,
        mode="plan",
    )
    answer = (
        PlanState.model_validate(_root_values(second)[-1]["tinkerfin_plan"])
        .clarification_history[0]
        .answers[0]
    )
    assert answer.option_id == "target-a"
    assert answer.answer == "Target A"


@pytest.mark.asyncio
async def test_clarification_rejects_incomplete_and_unknown_answers() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(
                responses=[
                    _planner_clarification(
                        question_id="target",
                        options=[{"id": "a", "label": "A"}],
                    )
                ]
            ),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="message")]},
        run_id="invalid-answer-1",
        config=config,
        mode="plan",
    )
    with pytest.raises(ValueError, match="unknown option"):
        await _parts(
            definition,
            Command(
                resume={
                    "type": "respond",
                    "answers": [{"questionId": "target", "optionId": "unknown"}],
                }
            ),
            run_id="invalid-answer-2",
            config=config,
            mode="plan",
        )


@pytest.mark.asyncio
async def test_clarification_fails_before_checkpoint_for_non_json_attributes() -> None:
    definition = (
        TinkerFin()
        .plan(
            enabled=True,
            clarification_schema=_OpaqueForm,
        )
        .create_deep_agent(
            model=_FakeModel(responses=[_opaque_planner_clarification()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    with pytest.raises(PlanStructuredOutputError, match="JSON checkpoint round-trip"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Plan", id="message")]},
            run_id="opaque",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
        )


@pytest.mark.asyncio
async def test_pending_clarification_rejects_schema_drift() -> None:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "plan-thread"}}
    original = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner_clarification()]),
            tools=[],
            checkpointer=saver,
        )
    )
    await _parts(
        original,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="schema-1",
        config=config,
        mode="plan",
    )
    changed = (
        TinkerFin()
        .plan(
            enabled=True,
            clarification_schema=_CustomForm,
        )
        .create_deep_agent(
            model=_FakeModel(responses=[AIMessage(content="unused")]),
            tools=[],
            checkpointer=saver,
        )
    )
    with pytest.raises(PlanModeConfigurationError, match="does not match"):
        await _parts(
            changed,
            Command(
                resume={
                    "type": "respond",
                    "answers": [{"questionId": "target", "answer": "A"}],
                }
            ),
            run_id="schema-2",
            config=config,
            mode="plan",
        )


@pytest.mark.asyncio
async def test_edit_is_authoritative_and_reenters_sufficiency_checks() -> None:
    model = _FakeModel(responses=[_planner(), _planner_clarification(), _accept_edit()])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="edit-1",
        config=config,
        mode="plan",
    )
    edited = {
        "goal": "Edited goal needs a target",
        "assumptions": [],
        "steps": [
            {
                "id": "edited-step",
                "title": "Edited",
                "description": "Wait for the target",
                "verification": ["Target is explicit"],
            }
        ],
        "acceptanceCriteria": ["Edited result works"],
    }
    clarification = await _parts(
        definition,
        Command(
            resume={
                "type": "edit",
                "baseRevision": 1,
                "draft": edited,
            }
        ),
        run_id="edit-2",
        config=config,
        mode="plan",
    )
    pending = PlanState.model_validate(
        _root_values(clarification)[-1]["tinkerfin_plan"]
    )
    assert pending.status is PlanStatus.AWAITING_CLARIFICATION
    assert pending.revision == 1
    assert pending.edited_draft is not None
    assert pending.edited_draft.goal == edited["goal"]
    reviewed = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [{"questionId": "target", "answer": "Target A"}],
            }
        ),
        run_id="edit-3",
        config=config,
        mode="plan",
    )
    final = PlanState.model_validate(_root_values(reviewed)[-1]["tinkerfin_plan"])
    assert final.revision == 2
    assert final.draft is not None and final.draft.goal == edited["goal"]
    assert final.draft.steps[0].id == "edited-step"
    assert final.edited_draft is None


@pytest.mark.asyncio
async def test_natural_language_feedback_creates_one_revised_draft() -> None:
    model = _FakeModel(responses=[_planner(), _planner(suffix=" revised")])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="feedback-1",
        config=config,
        mode="plan",
    )
    revised = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "baseRevision": 1,
                "message": "Add regression coverage",
            }
        ),
        run_id="feedback-2",
        config=config,
        mode="plan",
    )
    plan = PlanState.model_validate(_root_values(revised)[-1]["tinkerfin_plan"])
    assert plan.revision == 2
    assert plan.feedback == ("Add regression coverage",)
    assert plan.draft is not None and plan.draft.goal.endswith("revised")


@pytest.mark.asyncio
async def test_review_rejects_stale_revision_and_reject_ends_planning() -> None:
    config = {"configurable": {"thread_id": "plan-thread"}}
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="review-1",
        config=config,
        mode="plan",
    )
    with pytest.raises(ValueError, match="baseRevision is stale"):
        await _parts(
            definition,
            Command(resume={"type": "approve", "baseRevision": 2}),
            run_id="review-stale",
            config=config,
            mode="default",
        )

    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message-2")]},
        run_id="reject-1",
        config=config,
        mode="plan",
    )
    rejected = await _parts(
        definition,
        Command(resume={"type": "reject", "baseRevision": 1}),
        run_id="reject-2",
        config=config,
        mode="default",
    )
    plan = PlanState.model_validate(_root_values(rejected)[-1]["tinkerfin_plan"])
    assert plan.status is PlanStatus.CANCELLED
    assert plan.effective_mode == "default"


@pytest.mark.asyncio
async def test_plan_rejects_non_sync_durability() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    with pytest.raises(PlanModeConfigurationError, match="durability='sync'"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Plan", id="message")]},
            run_id="async-plan",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
            durability="async",
        )


@tool
def approved_tool(value: str) -> str:
    """Return a value after human review."""

    return value


@pytest.mark.asyncio
async def test_plan_handoff_tool_interrupt_resumes_as_native_default() -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_tool",
                        "args": {"value": "ok"},
                        "id": "approved-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="tool complete"),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[approved_tool],
            interrupt_on={"approved_tool": {"allowed_decisions": ["approve"]}},
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="tool-review",
        config=config,
        mode="plan",
    )
    plan_binding = _plan_binding(
        _terminal(review),
        run_id="tool-execution",
        payload={"type": "approve", "baseRevision": 1},
    )
    interrupted = await _agui_events(
        definition,
        plan_binding.command,
        run_id="tool-execution",
        config=config,
        mode="default",
        resume=plan_binding,
    )
    tool_terminal = _terminal(interrupted)
    tool_outcome = tool_terminal.outcome
    assert tool_outcome is not None and tool_outcome.type == "interrupt"
    tool_interrupt = tool_outcome.interrupts[0]
    assert tool_interrupt.reason == "tool_call"
    assert tool_interrupt.tool_call_id is not None
    kind, namespace, raw_id = ScopedIdCodec().decode(tool_interrupt.tool_call_id)
    assert (kind, namespace, raw_id) == ("tool", (), "approved-call")
    translation = ResumeMapper().map_agui(
        entries=(_resume_entry(tool_interrupt.id, {"type": "approve"}),),
        interrupts=tool_outcome.interrupts,
    )
    resume_binding = AgUiResumeBinding.from_translation(
        identity=_identity("tool-resume"),
        translation=translation,
    )
    completed = await _agui_events(
        definition,
        resume_binding.command,
        run_id="tool-resume",
        config=config,
        mode="default",
        resume=resume_binding,
    )
    completed_outcome = _terminal(completed).outcome
    assert completed_outcome is not None and completed_outcome.type == "success"
    completed_snapshots = [
        event for event in completed if isinstance(event, StateSnapshotEvent)
    ]
    assert completed_snapshots
    resumed_plan = PlanState.model_validate(
        completed_snapshots[0].snapshot["tinkerfin_plan"]
    )
    assert resumed_plan.status is PlanStatus.APPROVED
    assert resumed_plan.effective_mode == "default"
    assert resumed_plan.handoff is not None and resumed_plan.handoff.dispatched


@pytest.mark.asyncio
async def test_agui_interrupt_snapshots_precede_the_terminal() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="agui-review",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    types = [event.type.value for event in events]
    state_index = len(types) - 1 - types[::-1].index("STATE_SNAPSHOT")
    messages_index = len(types) - 1 - types[::-1].index("MESSAGES_SNAPSHOT")
    terminal_index = len(types) - 1 - types[::-1].index("RUN_FINISHED")
    assert state_index < messages_index < terminal_index
    snapshots = [event for event in events if isinstance(event, StateSnapshotEvent)]
    assert all(
        "_tinkerfin_plan_clarification_schema" not in event.snapshot
        for event in snapshots
    )


@pytest.mark.asyncio
async def test_read_only_planner_inherits_store_and_has_no_write_tools() -> None:
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/context.txt"},
                        "id": "planner-read",
                        "type": "tool_call",
                    }
                ],
            ),
            _planner(),
        ]
    )
    store = InMemoryStore()
    store.put(
        ("planner-files",),
        "/context.txt",
        dict(create_file_data("planning context")),
    )
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            backend=StoreBackend(namespace=lambda _runtime: ("planner-files",)),
            checkpointer=InMemorySaver(),
            store=store,
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-store",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert any(
        isinstance(cast(tuple[BaseMessage, object], part["data"])[0], ToolMessage)
        and "planning context"
        in str(cast(tuple[BaseMessage, object], part["data"])[0].content)
        for part in parts
        if part["type"] == "messages" and part["ns"] != ()
    )
    planner_bindings = [
        set(names) for names in model.bound_tool_names if "PlannerOutcome" in names
    ]
    assert planner_bindings
    assert all(
        {"ls", "read_file", "glob", "grep", "PlannerOutcome"} <= names
        for names in planner_bindings
    )
    assert all(
        {"write_file", "edit_file", "delete", "execute"}.isdisjoint(names)
        for names in planner_bindings
    )


@pytest.mark.asyncio
async def test_read_only_planner_has_a_bounded_model_call_budget() -> None:
    repeated: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ls",
                    "args": {"path": "/"},
                    "id": f"ls-{index}",
                    "type": "tool_call",
                }
            ],
        )
        for index in range(6)
    ]
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=repeated),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    with pytest.raises(ModelCallLimitExceededError, match=r"run limit \(6/6\)"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Inspect", id="message")]},
            run_id="budget",
            config={"configurable": {"thread_id": "plan-thread"}},
            mode="plan",
        )


@pytest.mark.asyncio
async def test_planner_and_native_preserve_runtime_context() -> None:
    model = _ContextAwareModel(responses=[_planner(), AIMessage(content="done")])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            context_schema=_RuntimeContext,
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="context-1",
        config=config,
        mode="plan",
        context={"tenant": "tenant-1"},
    )
    await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="context-2",
        config=config,
        mode="default",
        context={"tenant": "tenant-1"},
    )
    assert model.contexts == ({"tenant": "tenant-1"}, {"tenant": "tenant-1"})
