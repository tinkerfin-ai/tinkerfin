"""Public Plan Mode contracts and parent/subgraph lifecycle behavior."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypedDict, cast

import pytest
from ag_ui.core import (
    BaseEvent,
    RunFinishedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.core.types import ResumeEntry
from deepagents import create_deep_agent
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from deepagents.graph import DeepAgentState
from langchain.agents.middleware import TodoListMiddleware, wrap_model_call
from langchain.agents.middleware.model_call_limit import (
    ModelCallLimitExceededError,
)
from langchain.tools import ToolRuntime, tool
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
from langgraph.runtime import get_runtime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Interrupt
from pydantic import Field, PrivateAttr, ValidationError, field_serializer

import tinkerfin.plan as plan_api
from tinkerfin import AgUiResumeBinding, Identity, TinkerFin
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationFormBase,
    ClarificationModel,
    ClarificationOption,
    ClarificationOptionBase,
    ClarificationQuestion,
    ClarificationQuestionBase,
    DefaultClarificationForm,
    PlanDraft,
    PlanModeConfigurationError,
    PlanState,
    PlanStep,
    PlanStructuredOutputError,
)
from tinkerfin_agui_adapter import ResumeMapper


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


class _CustomState(DeepAgentState):
    tenant_marker: str


class _GlobalState(DeepAgentState):
    global_marker: str


class _DefinitionState(DeepAgentState):
    definition_marker: str


class _RuntimeContext(TypedDict):
    tenant: str


class _ContextAwareFakeModel(_FakeModel):
    _runtime_contexts: list[_RuntimeContext] = PrivateAttr(default_factory=list)

    @property
    def runtime_contexts(self) -> tuple[_RuntimeContext, ...]:
        return tuple(self._runtime_contexts)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        context = get_runtime(_RuntimeContext).context
        assert context is not None
        self._runtime_contexts.append(context)
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


class _CustomQuestionAttributes(ClarificationModel):
    category: str


class _CustomOptionAttributes(ClarificationModel):
    priority: int


class _CustomOption(ClarificationOption[_CustomOptionAttributes]):
    pass


class _CustomQuestion(
    ClarificationQuestion[_CustomQuestionAttributes, _CustomOptionAttributes]
):
    options: tuple[_CustomOption, ...] = ()


class _CustomClarificationForm(ClarificationForm[_CustomQuestion]):
    pass


class _DictOption(ClarificationOptionBase):
    attributes: dict[str, str] | None = None


class _DictQuestion(ClarificationQuestionBase):
    options: tuple[_DictOption, ...] = ()


class _DictClarificationForm(ClarificationForm[_DictQuestion]):
    pass


class _AnyOption(ClarificationOptionBase):
    attributes: Any = None


class _AnyQuestion(ClarificationQuestionBase):
    options: tuple[_AnyOption, ...] = ()


class _AnyClarificationForm(ClarificationForm[_AnyQuestion]):
    pass


class _BadCoreOption(ClarificationOptionBase):
    id: int


class _BadCoreOptionQuestion(ClarificationQuestionBase):
    options: tuple[_BadCoreOption, ...] = ()


class _BadCoreOptionForm(ClarificationForm[_BadCoreOptionQuestion]):
    pass


class _BadCoreQuestion(ClarificationQuestionBase):
    id: int


class _BadCoreQuestionForm(ClarificationForm[_BadCoreQuestion]):
    pass


class _ListQuestionForm(ClarificationFormBase):
    questions: list[_CustomQuestion]


class _ListOptionQuestion(ClarificationQuestionBase):
    options: list[_CustomOption] = Field(default_factory=list)


class _ListOptionForm(ClarificationForm[_ListOptionQuestion]):
    pass


class _CustomQuestionA(_CustomQuestion):
    type: Literal["a"] = "a"


class _CustomQuestionB(_CustomQuestion):
    type: Literal["b"] = "b"


_CustomQuestionUnion = Annotated[
    _CustomQuestionA | _CustomQuestionB,
    Field(discriminator="type"),
]


class _MixedClarificationForm(ClarificationFormBase):
    questions: tuple[_CustomQuestionUnion, ...]


class _OpaqueQuestionAttributes(ClarificationModel):
    token: str

    @field_serializer("token")
    def serialize_token(self, value: str) -> object:
        del value
        return object()


class _OpaqueQuestion(
    ClarificationQuestion[_OpaqueQuestionAttributes, ClarificationModel]
):
    pass


class _OpaqueClarificationForm(ClarificationForm[_OpaqueQuestion]):
    pass


def _identity(run_id: str) -> Identity:
    return Identity(threadId="plan-thread", runId=run_id)


def _gate(
    route: str,
    *,
    questions: list[dict[str, object]] | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "GateDecision",
                "args": {
                    "route": route,
                    "goal": "Implement feature",
                    "clarification": (
                        None
                        if questions is None
                        else {"schema_version": 1, "questions": questions}
                    ),
                },
                "id": f"gate-{route}",
                "type": "tool_call",
            }
        ],
    )


def _planner(*, suffix: str = "") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "clarification": None,
                    "draft": {
                        "goal": f"Implement feature{suffix}",
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
    )


def _planner_clarification() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "clarification": {
                        "schema_version": 1,
                        "questions": [
                            {
                                "id": "planner-q-1",
                                "prompt": "Which environment?",
                                "options": [
                                    {
                                        "id": "staging",
                                        "label": "Staging",
                                        "description": "Use the test environment",
                                    },
                                    {
                                        "id": "production",
                                        "label": "Production",
                                        "description": "Use the live environment",
                                    },
                                ],
                                "allow_free_text": False,
                            }
                        ],
                    },
                    "draft": None,
                },
                "id": "planner-clarification",
                "type": "tool_call",
            }
        ],
    )


def _custom_gate_clarification(*, priority: object = 1) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "GateDecision",
                "args": {
                    "route": "clarify",
                    "goal": "Implement feature",
                    "clarification": {
                        "schema_version": 1,
                        "questions": [
                            {
                                "id": "custom-q-1",
                                "prompt": "Choose the target",
                                "attributes": {"category": "target"},
                                "options": [
                                    {
                                        "id": "custom-option-a",
                                        "label": "Target A",
                                        "description": "Use target A",
                                        "attributes": {"priority": priority},
                                    }
                                ],
                                "allow_free_text": False,
                            }
                        ],
                    },
                },
                "id": "gate-custom-clarification",
                "type": "tool_call",
            }
        ],
    )


def _opaque_gate_clarification() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "GateDecision",
                "args": {
                    "route": "clarify",
                    "goal": "Implement feature",
                    "clarification": {
                        "questions": [
                            {
                                "id": "opaque-q",
                                "prompt": "Choose",
                                "attributes": {"token": "secret"},
                            }
                        ]
                    },
                },
                "id": "gate-opaque-clarification",
                "type": "tool_call",
            }
        ],
    )


async def _parts(
    definition: object,
    graph_input: object,
    *,
    run_id: str,
    config: Mapping[str, object],
    durability: str | None = None,
    mode: str | None = None,
    context: object | None = None,
) -> list[Mapping[str, object]]:
    runtime = cast(Any, definition).new(identity=_identity(run_id), mode=mode)
    kwargs: dict[str, object] = {
        "config": config,
        "stream_mode": ["messages", "tasks", "values"],
        "subgraphs": True,
    }
    if durability is not None:
        kwargs["durability"] = durability
    if context is not None:
        kwargs["context"] = context
    return [part async for part in runtime.astream(graph_input, **kwargs)]


async def _agui_events(
    definition: object,
    graph_input: object,
    *,
    run_id: str,
    config: Mapping[str, object],
    resume: AgUiResumeBinding | None = None,
    mode: str | None = None,
) -> list[BaseEvent]:
    identity = _identity(run_id)
    runtime = cast(Any, definition).new_agui(
        identity=identity,
        resume=resume,
        mode=mode,
    )
    return [
        event
        async for event in runtime.astream(
            graph_input,
            config=config,
        )
    ]


def _resume_entry(
    interrupt_id: str,
    payload: Mapping[str, object],
) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": "resolved",
            "payload": dict(payload),
        }
    )


def _terminal(events: Sequence[BaseEvent]) -> RunFinishedEvent:
    terminals = [event for event in events if isinstance(event, RunFinishedEvent)]
    assert len(terminals) == 1
    return terminals[0]


def _root_values(parts: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [
        cast(Mapping[str, object], part["data"])
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]


def _root_interrupts(parts: Sequence[Mapping[str, object]]) -> tuple[Interrupt, ...]:
    values = [
        cast(tuple[Interrupt, ...], part.get("interrupts", ()))
        for part in parts
        if part["type"] == "values" and part["ns"] == ()
    ]
    return values[-1]


def test_plan_configuration_is_immutable_and_preserves_upstream_signature() -> None:
    base = TinkerFin()
    planned = base.plan(enabled=True, default_mode="plan")

    assert planned is not base
    assert inspect.signature(base.create_deep_agent) == inspect.signature(
        create_deep_agent
    )
    assert inspect.signature(planned.create_deep_agent) == inspect.signature(
        create_deep_agent
    )
    base.create_deep_agent(model=_FakeModel(responses=[AIMessage(content="ok")]))
    with pytest.raises(PlanModeConfigurationError, match="BaseCheckpointSaver"):
        planned.create_deep_agent(model=_FakeModel(responses=[AIMessage(content="ok")]))


def test_plan_package_exports_the_complete_clarification_contract() -> None:
    expected = {
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
        "PlanDraft",
        "PlanModeConfigurationError",
        "PlanReviewAction",
        "PlanRoute",
        "PlanState",
        "PlanStatus",
        "PlanStep",
        "PlanStructuredOutputError",
        "RequirementAnswer",
    }

    assert set(plan_api.__all__) == expected


@pytest.mark.asyncio
async def test_plan_configuration_preserves_coordinator_and_definition_topology() -> (
    None
):
    coordinated: list[Identity] = []

    @asynccontextmanager
    async def coordinate(identity: Identity) -> AsyncIterator[None]:
        coordinated.append(identity)
        yield

    base = TinkerFin(run_coordinator=coordinate)
    planned = base.plan(enabled=True, default_mode="plan")
    definition = planned.create_deep_agent(
        model=_FakeModel(responses=[_gate("direct"), AIMessage(content="done")]),
        tools=[],
        checkpointer=InMemorySaver(),
    )
    planned.plan(enabled=False)

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="coordinated-plan",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    assert coordinated == [_identity("coordinated-plan")]
    plan = PlanState.model_validate(_root_values(parts)[-1]["tinkerfin_plan"])
    assert plan.status.value == "completed"
    assert plan.route is not None and plan.route.value == "direct"


@pytest.mark.parametrize("value", [1, "true", None, object()])
def test_plan_configuration_requires_a_strict_bool(value: object) -> None:
    with pytest.raises(TypeError, match="enabled must be a bool"):
        TinkerFin().plan(enabled=cast(Any, value))


def test_plan_configuration_validates_disabled_options_and_modes() -> None:
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().plan(enabled=False, default_mode="plan")
    with pytest.raises(PlanModeConfigurationError, match="default_mode"):
        TinkerFin().plan(default_mode=cast(Any, "automatic"))
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().plan(
            enabled=False,
            clarification_schema=_CustomClarificationForm,
        )


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (ClarificationModel, "ClarificationFormBase subclass"),
        (ClarificationForm, "unbound generics"),
        (_DictClarificationForm, "ClarificationModel"),
        (_AnyClarificationForm, "ClarificationModel"),
        (_BadCoreOptionForm, "core fields unchanged"),
        (_BadCoreQuestionForm, "core fields unchanged"),
        (_ListQuestionForm, "variadic tuple"),
        (_ListOptionForm, "variadic tuple"),
    ],
)
def test_plan_configuration_rejects_invalid_clarification_schemas(
    schema: object,
    message: str,
) -> None:
    with pytest.raises(PlanModeConfigurationError, match=message):
        TinkerFin().plan(clarification_schema=cast(Any, schema))


def test_plan_configuration_accepts_a_concrete_discriminated_question_union() -> None:
    configured = TinkerFin().plan(clarification_schema=_MixedClarificationForm)
    form = _MixedClarificationForm.model_validate(
        {
            "questions": [
                {
                    "type": "a",
                    "id": "question-a",
                    "prompt": "Choose",
                    "attributes": {"category": "target"},
                }
            ]
        }
    )

    assert configured is not None
    assert isinstance(form.questions[0], _CustomQuestionA)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        _CustomClarificationForm.model_validate(
            {
                "questions": [
                    {
                        "id": "question",
                        "prompt": "Choose",
                        "attributes": {
                            "category": "target",
                            "unknown": "rejected",
                        },
                    }
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
async def test_plan_definition_switches_modes_in_one_checkpoint_thread() -> None:
    model = _FakeModel(
        responses=[
            AIMessage(content="default done"),
            _gate("direct"),
            AIMessage(content="plan done"),
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
    config: RunnableConfig = {"configurable": {"thread_id": "plan-thread"}}

    default_parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Answer directly")]},
        run_id="mode-default",
        config=config,
        mode="default",
    )
    plan_parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Use Plan routing")]},
        run_id="mode-plan",
        config=config,
        mode="plan",
    )

    default_state = PlanState.model_validate(
        _root_values(default_parts)[-1]["tinkerfin_plan"]
    )
    plan_state = PlanState.model_validate(
        _root_values(plan_parts)[-1]["tinkerfin_plan"]
    )
    assert default_state.route is None
    assert plan_state.route is not None and plan_state.route.value == "direct"
    assert len(model.model_inputs) == 3


@pytest.mark.asyncio
async def test_default_input_can_follow_an_abandoned_pending_plan() -> None:
    model = _FakeModel(
        responses=[
            _gate("plan"),
            _planner(),
            AIMessage(content="default after abandon"),
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
    config: RunnableConfig = {"configurable": {"thread_id": "plan-thread"}}

    pending = await _parts(
        definition,
        {"messages": [HumanMessage(content="Create a Plan")]},
        run_id="abandon-plan",
        config=config,
        mode="plan",
    )
    assert _root_interrupts(pending)[0].value["kind"] == "plan_review"

    completed = await _parts(
        definition,
        {"messages": [HumanMessage(content="Continue without a Plan")]},
        run_id="after-abandon",
        config=config,
        mode="default",
    )

    assert _root_interrupts(completed) == ()
    final = _root_values(completed)[-1]
    plan = PlanState.model_validate(final["tinkerfin_plan"])
    assert plan.status.value == "completed"
    assert plan.route is None
    assert cast(Sequence[BaseMessage], final["messages"])[-1].content == (
        "default after abandon"
    )
    assert len(model.model_inputs) == 3


@pytest.mark.asyncio
async def test_global_and_definition_state_schemas_are_composed() -> None:
    definition = TinkerFin(state_schema=_GlobalState).create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="done")]),
        tools=[],
        state_schema=_DefinitionState,
    )
    parts = await _parts(
        definition,
        {
            "messages": [HumanMessage(content="Run")],
            "global_marker": "global",
            "definition_marker": "definition",
        },
        run_id="composed-global-state",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    final = _root_values(parts)[-1]
    assert final["global_marker"] == "global"
    assert final["definition_marker"] == "definition"


@pytest.mark.asyncio
async def test_plan_parent_exposes_files_and_todos_on_the_default_route() -> None:
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_todos",
                        "args": {
                            "todos": [
                                {"content": "Record output", "status": "in_progress"}
                            ]
                        },
                        "id": "todos-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "/result.txt", "content": "done"},
                        "id": "write-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
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

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Create the result")]},
        run_id="root-files-todos",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="default",
    )

    final = _root_values(parts)[-1]
    assert final["todos"] == [{"content": "Record output", "status": "in_progress"}]
    assert "/result.txt" in cast(Mapping[str, object], final["files"])


def test_runtime_rejects_overriding_its_bound_mode() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[AIMessage(content="done")]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    runtime = definition.new(identity=_identity("reserved-mode"), mode="default")

    with pytest.raises(ValueError, match="reserves 'tinkerfin_plan_mode'"):
        runtime.astream(
            {"messages": [HumanMessage(content="Run")]},
            config={
                "configurable": {
                    "thread_id": "plan-thread",
                    "tinkerfin_plan_mode": "plan",
                }
            },
        )


def test_clarification_options_are_dynamic_and_validated() -> None:
    question = ClarificationQuestion(
        id="deployment",
        prompt="Where should this deploy?",
        options=(
            ClarificationOption(
                id="staging",
                label="Staging",
                description="Deploy to the test environment",
            ),
        ),
        allow_free_text=False,
    )

    assert question.options[0].id == "staging"
    with pytest.raises(ValidationError, match="require options"):
        ClarificationQuestion(
            id="deployment",
            prompt="Where should this deploy?",
            allow_free_text=False,
        )

    option = ClarificationOption(id="duplicate", label="Duplicate")
    with pytest.raises(ValidationError, match="option IDs must be unique"):
        ClarificationQuestion(
            id="deployment",
            prompt="Where should this deploy?",
            options=(option, option),
        )
    with pytest.raises(ValidationError, match="question IDs must be unique"):
        DefaultClarificationForm.model_validate(
            {
                "questions": [
                    {"id": "duplicate", "prompt": "First question"},
                    {"id": "duplicate", "prompt": "Second question"},
                ]
            }
        )


@pytest.mark.asyncio
async def test_gate_and_deep_agent_preserve_the_parent_runtime_context() -> None:
    model = _ContextAwareFakeModel(
        responses=[_gate("direct"), AIMessage(content="done")]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            context_schema=_RuntimeContext,
            checkpointer=InMemorySaver(),
        )
    )
    context: _RuntimeContext = {"tenant": "tenant-1"}

    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="runtime-context",
        config={"configurable": {"thread_id": "plan-thread"}},
        context=context,
    )

    assert model.runtime_contexts == (context, context)


def test_plan_mode_requires_an_explicit_model_and_concrete_saver() -> None:
    planned = TinkerFin().plan(enabled=True, default_mode="plan")

    with pytest.raises(PlanModeConfigurationError, match="explicit model"):
        planned.create_deep_agent(checkpointer=InMemorySaver())
    for invalid_model in ("", " "):
        with pytest.raises(PlanModeConfigurationError, match="explicit model"):
            planned.create_deep_agent(
                model=invalid_model,
                checkpointer=InMemorySaver(),
            )
    for invalid in (None, True, False):
        with pytest.raises(PlanModeConfigurationError, match="BaseCheckpointSaver"):
            planned.create_deep_agent(
                model=_FakeModel(responses=[AIMessage(content="ok")]),
                checkpointer=cast(Any, invalid),
            )


def test_plan_contracts_are_frozen_and_reject_duplicate_step_ids() -> None:
    step = PlanStep(
        id="step-1",
        title="Implement",
        description="Implement feature",
        verification=("Tests pass",),
    )
    draft = PlanDraft(
        revision=1,
        goal="Implement feature",
        steps=(step,),
        acceptance_criteria=("Feature works",),
    )

    with pytest.raises(ValidationError):
        draft.revision = 2  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError, match="step IDs must be unique"):
        PlanDraft(
            revision=1,
            goal="Implement feature",
            steps=(step, step),
            acceptance_criteria=("Feature works",),
        )
    with pytest.raises(ValidationError, match="valid integer"):
        PlanDraft(
            revision=cast(Any, "1"),
            goal="Implement feature",
            steps=(step,),
            acceptance_criteria=("Feature works",),
        )


@pytest.mark.asyncio
async def test_plan_parent_preserves_custom_deep_agent_state_fields() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(responses=[_gate("direct"), AIMessage(content="done")]),
            tools=[],
            state_schema=_CustomState,
            checkpointer=InMemorySaver(),
        )
    )

    parts = await _parts(
        definition,
        {
            "messages": [HumanMessage(content="Implement")],
            "tenant_marker": "tenant-1",
        },
        run_id="custom-state",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    final = _root_values(parts)[-1]
    assert final["tenant_marker"] == "tenant-1"
    assert PlanState.model_validate(final["tinkerfin_plan"]).status.value == "completed"


@pytest.mark.asyncio
async def test_plan_review_approve_executes_child_with_json_checkpoint_state() -> None:
    model = _FakeModel(responses=[_gate("plan"), _planner(), AIMessage(content="done")])
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=saver,
        )
    )
    config: RunnableConfig = {"configurable": {"thread_id": "plan-thread"}}

    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="run-1",
        config=config,
    )
    second = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="run-2",
        config=config,
    )

    plan_interrupt = _root_interrupts(first)[0]
    assert plan_interrupt.value["kind"] == "plan_review"
    final = _root_values(second)[-1]
    plan = PlanState.model_validate(final["tinkerfin_plan"])
    assert plan.status.value == "completed"
    assert plan.confirmed_plan is not None
    conversation = cast(Sequence[BaseMessage], final["messages"])
    assert conversation[-1].content == "done"
    assert not any(isinstance(message, SystemMessage) for message in conversation)
    execution_system = next(
        message
        for message in model.model_inputs[-1]
        if isinstance(message, SystemMessage)
    )
    assert "user-approved Plan" in str(execution_system.content)
    assert '"goal": "Implement feature"' in str(execution_system.content)
    namespaces = {
        cast(
            str,
            checkpoint.config.get("configurable", {}).get("checkpoint_ns", ""),
        )
        for checkpoint in saver.list(config)
    }
    assert "" in namespaces
    assert not any(namespace.startswith("plan_gate:") for namespace in namespaces)
    assert not any(namespace.startswith("create_plan:") for namespace in namespaces)
    assert any(namespace.startswith("execute_deep_agent:") for namespace in namespaces)
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
async def test_read_only_planner_inherits_the_parent_store() -> None:
    model = _FakeModel(
        responses=[
            _gate("plan"),
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
    backend = StoreBackend(namespace=lambda _runtime: ("planner-files",))
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            backend=backend,
            checkpointer=InMemorySaver(),
            store=store,
        )
    )

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="planner-store",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    tool_messages = [
        cast(tuple[BaseMessage, object], part["data"])[0]
        for part in parts
        if part["type"] == "messages" and part["ns"] != ()
    ]
    assert any(
        isinstance(message, ToolMessage)
        and message.name == "read_file"
        and "planning context" in str(message.content)
        for message in tool_messages
    )
    assert _root_interrupts(parts)[0].value["kind"] == "plan_review"


@pytest.mark.asyncio
async def test_read_only_planner_has_a_bounded_model_call_budget() -> None:
    repeated_inspection = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ls",
                    "args": {"path": "/"},
                    "id": f"planner-ls-{index}",
                    "type": "tool_call",
                }
            ],
        )
        for index in range(6)
    ]
    model = _FakeModel(responses=[_gate("plan"), *repeated_inspection])
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )

    with pytest.raises(ModelCallLimitExceededError, match=r"run limit \(6/6\)"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Inspect forever")]},
            run_id="planner-model-call-limit",
            config={"configurable": {"thread_id": "plan-thread"}},
        )

    assert len(model.model_inputs) == 7


@pytest.mark.asyncio
async def test_direct_route_skips_planner_and_review() -> None:
    model = _FakeModel(responses=[_gate("direct"), AIMessage(content="direct done")])
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Do the simple task")]},
        run_id="run-direct",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    assert _root_interrupts(parts) == ()
    plan = PlanState.model_validate(_root_values(parts)[-1]["tinkerfin_plan"])
    assert plan.status.value == "completed"
    assert plan.route is not None and plan.route.value == "direct"
    assert plan.draft is None
    assert "user-approved Plan" not in "\n".join(
        str(message.content)
        for message in model.model_inputs[-1]
        if isinstance(message, SystemMessage)
    )


@pytest.mark.asyncio
async def test_clarification_answers_are_required_before_planning() -> None:
    model = _FakeModel(
        responses=[
            _gate(
                "clarify",
                questions=[{"id": "q-1", "prompt": "Which target?"}],
            ),
            _gate("plan"),
            _planner(),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="clarify-1",
        config=config,
    )
    clarification = _root_interrupts(first)[0].value
    assert clarification["kind"] == "plan_clarification"
    assert clarification["metadata"]["source"] == "gate"

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [{"questionId": "q-1", "answer": "Target A"}],
            }
        ),
        run_id="clarify-2",
        config=config,
    )
    assert _root_interrupts(second)[0].value["kind"] == "plan_review"
    plan = PlanState.model_validate(_root_values(second)[-1]["tinkerfin_plan"])
    assert plan.clarification_history[0].answers[0].answer == "Target A"


@pytest.mark.asyncio
async def test_custom_clarification_form_round_trips_attributes_and_option_id() -> None:
    model = _FakeModel(
        responses=[
            _custom_gate_clarification(),
            _gate("plan"),
            _planner(),
        ]
    )
    saver = InMemorySaver(
        serde=JsonPlusSerializer(pickle_fallback=False),
    )
    definition = (
        TinkerFin()
        .plan(
            enabled=True,
            default_mode="plan",
            clarification_schema=_CustomClarificationForm,
        )
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=saver,
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}

    interrupted = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="custom-form-1",
        config=config,
    )
    pending = PlanState.model_validate(
        _root_values(interrupted)[-1]["tinkerfin_plan"]
    ).pending_clarification
    assert pending is not None
    question = cast(Sequence[Mapping[str, object]], pending.form["questions"])[0]
    assert question["attributes"] == {"category": "target"}
    option = cast(Sequence[Mapping[str, object]], question["options"])[0]
    assert option["attributes"] == {"priority": 1}

    native_interrupt = _root_interrupts(interrupted)[0]
    metadata = cast(Mapping[str, object], native_interrupt.value["metadata"])
    clarification = cast(Mapping[str, object], metadata["clarification"])
    assert "schemaFingerprint" not in clarification

    reviewed = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [
                    {
                        "questionId": "custom-q-1",
                        "optionId": "custom-option-a",
                    }
                ],
            }
        ),
        run_id="custom-form-2",
        config=config,
    )
    plan = PlanState.model_validate(_root_values(reviewed)[-1]["tinkerfin_plan"])
    exchange = plan.clarification_history[0]
    assert exchange.answers[0].answer == "Target A"
    assert exchange.answers[0].option_id == "custom-option-a"
    assert exchange.form == pending.form
    assert any(
        '"priority": 1' in str(message.content)
        for model_input in model.model_inputs
        for message in model_input
        if isinstance(message, HumanMessage)
    )


@pytest.mark.asyncio
async def test_ag_ui_custom_form_preserves_attributes_without_internal_fingerprint() -> (
    None
):
    definition = (
        TinkerFin()
        .plan(
            enabled=True,
            default_mode="plan",
            clarification_schema=_CustomClarificationForm,
        )
        .create_deep_agent(
            model=_FakeModel(responses=[_custom_gate_clarification()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )

    events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="custom-form-agui",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    terminal = _terminal(events)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    interrupt = terminal.outcome.interrupts[0]
    persisted = cast(Mapping[str, object], interrupt.metadata)["runtimeInterrupt"]
    envelope = cast(Mapping[str, object], persisted)["envelope"]
    metadata = cast(Mapping[str, object], envelope)["metadata"]
    clarification = cast(Mapping[str, object], metadata)["clarification"]
    form = cast(Mapping[str, object], clarification)["form"]
    question = cast(
        Sequence[Mapping[str, object]], cast(Mapping[str, object], form)["questions"]
    )[0]
    option = cast(Sequence[Mapping[str, object]], question["options"])[0]
    assert question["attributes"] == {"category": "target"}
    assert option["attributes"] == {"priority": 1}
    assert "schemaFingerprint" not in cast(Mapping[str, object], clarification)
    snapshots = [event for event in events if isinstance(event, StateSnapshotEvent)]
    assert snapshots
    assert all(
        "_tinkerfin_plan_clarification_schema" not in snapshot.snapshot
        for snapshot in snapshots
    )
    assert all(
        "_tinkerfin_plan_clarification_schema"
        not in event.model_dump_json(by_alias=True)
        for event in events
    )


@pytest.mark.asyncio
async def test_concrete_tool_strategy_retries_invalid_custom_attributes() -> None:
    model = _FakeModel(
        responses=[
            _custom_gate_clarification(priority="invalid"),
            _custom_gate_clarification(priority=1),
        ]
    )
    definition = (
        TinkerFin()
        .plan(
            enabled=True,
            default_mode="plan",
            clarification_schema=_CustomClarificationForm,
        )
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )

    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="custom-retry",
        config={"configurable": {"thread_id": "plan-thread"}},
    )

    assert _root_interrupts(parts)[0].value["kind"] == "plan_clarification"
    assert len(model.model_inputs) == 2
    assert any(
        isinstance(message, ToolMessage)
        and "Failed to parse structured output" in str(message.content)
        for message in model.model_inputs[1]
    )


@pytest.mark.asyncio
async def test_clarification_fails_before_checkpoint_on_invalid_json_serializer() -> (
    None
):
    definition = (
        TinkerFin()
        .plan(
            enabled=True,
            default_mode="plan",
            clarification_schema=_OpaqueClarificationForm,
        )
        .create_deep_agent(
            model=_FakeModel(responses=[_opaque_gate_clarification()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )

    with pytest.raises(PlanStructuredOutputError, match="JSON checkpoint round-trip"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Implement")]},
            run_id="opaque-form",
            config={"configurable": {"thread_id": "plan-thread"}},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"questionId": "q-1", "optionId": "a", "answer": "tampered"},
        {"questionId": "q-1", "optionId": "a", "answer": None},
        {"questionId": "q-1", "optionId": None, "answer": "custom"},
        {"questionId": "q-1"},
    ],
)
async def test_clarification_response_schema_enforces_two_exact_answer_shapes(
    answer: dict[str, object],
) -> None:
    config = {"configurable": {"thread_id": "plan-thread"}}
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(
                responses=[
                    _gate(
                        "clarify",
                        questions=[
                            {
                                "id": "q-1",
                                "prompt": "Which target?",
                                "options": [
                                    {"id": "a", "label": "A"},
                                ],
                            }
                        ],
                    )
                ]
            ),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    interrupted = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="exclusive-answer-1",
        config=config,
    )
    response_schema = _root_interrupts(interrupted)[0].value["responseSchema"]
    definitions = response_schema["$defs"]
    answer_shapes = {
        frozenset(answer_definition["required"])
        for answer_definition in definitions.values()
    }
    assert answer_shapes == {
        frozenset({"questionId", "optionId"}),
        frozenset({"questionId", "answer"}),
    }
    assert all(
        answer_definition["additionalProperties"] is False
        for answer_definition in definitions.values()
    )

    with pytest.raises(ValidationError):
        await _parts(
            definition,
            Command(
                resume={
                    "type": "respond",
                    "answers": [answer],
                }
            ),
            run_id="exclusive-answer-2",
            config=config,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "message"),
    [
        (
            [{"questionId": "q-option", "optionId": "a"}],
            "every pending question",
        ),
        (
            [
                {"questionId": "q-option", "optionId": "a"},
                {"questionId": "old-question", "answer": "stale"},
            ],
            "every pending question",
        ),
        (
            [
                {"questionId": "q-option", "optionId": "a"},
                {"questionId": "q-option", "optionId": "a"},
                {"questionId": "q-text", "answer": "details"},
            ],
            "question IDs must be unique",
        ),
        (
            [
                {"questionId": "q-option", "optionId": "old-option"},
                {"questionId": "q-text", "answer": "details"},
            ],
            "unknown option",
        ),
        (
            [
                {"questionId": "q-option", "answer": "custom"},
                {"questionId": "q-text", "answer": "details"},
            ],
            "requires an option",
        ),
    ],
)
async def test_clarification_rejects_incomplete_duplicate_and_stale_answers(
    answers: list[dict[str, str]],
    message: str,
) -> None:
    config = {"configurable": {"thread_id": "plan-thread"}}
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(
                responses=[
                    _gate(
                        "clarify",
                        questions=[
                            {
                                "id": "q-option",
                                "prompt": "Choose a target",
                                "options": [{"id": "a", "label": "Target A"}],
                                "allow_free_text": False,
                            },
                            {
                                "id": "q-text",
                                "prompt": "Add details",
                            },
                        ],
                    )
                ]
            ),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="invalid-answer-1",
        config=config,
    )

    with pytest.raises((ValidationError, ValueError), match=message):
        await _parts(
            definition,
            Command(resume={"type": "respond", "answers": answers}),
            run_id="invalid-answer-2",
            config=config,
        )


@pytest.mark.asyncio
async def test_pending_clarification_rejects_definition_schema_drift() -> None:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "plan-thread"}}
    original = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(
                responses=[
                    _gate(
                        "clarify",
                        questions=[{"id": "q-1", "prompt": "Which target?"}],
                    )
                ]
            ),
            tools=[],
            checkpointer=saver,
        )
    )
    await _parts(
        original,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="schema-drift-1",
        config=config,
    )
    changed = (
        TinkerFin()
        .plan(
            enabled=True,
            default_mode="plan",
            clarification_schema=_CustomClarificationForm,
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
                    "answers": [{"questionId": "q-1", "answer": "A"}],
                }
            ),
            run_id="schema-drift-2",
            config=config,
        )


@pytest.mark.asyncio
async def test_planner_can_request_clarification_before_creating_a_draft() -> None:
    model = _FakeModel(
        responses=[
            _gate("plan"),
            _planner_clarification(),
            _planner(),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    clarification = await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="planner-clarify-1",
        config=config,
    )
    interrupt_value = _root_interrupts(clarification)[0].value
    assert interrupt_value["kind"] == "plan_clarification"
    assert interrupt_value["metadata"]["source"] == "planner"
    clarification_metadata = interrupt_value["metadata"]["clarification"]
    assert clarification_metadata["schema"] == "tinkerfin.plan-clarification.v1"
    assert "schemaFingerprint" not in clarification_metadata
    assert clarification_metadata["form"] == {
        "schemaVersion": 1,
        "questions": [
            {
                "id": "planner-q-1",
                "prompt": "Which environment?",
                "options": [
                    {
                        "id": "staging",
                        "label": "Staging",
                        "description": "Use the test environment",
                        "attributes": None,
                    },
                    {
                        "id": "production",
                        "label": "Production",
                        "description": "Use the live environment",
                        "attributes": None,
                    },
                ],
                "allowFreeText": False,
                "attributes": None,
            }
        ],
    }
    pending = PlanState.model_validate(
        _root_values(clarification)[-1]["tinkerfin_plan"]
    )
    assert pending.revision == 0
    assert pending.draft is None
    assert pending.pending_clarification is not None

    review = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [
                    {
                        "questionId": "planner-q-1",
                        "optionId": "staging",
                    }
                ],
            }
        ),
        run_id="planner-clarify-2",
        config=config,
    )

    assert _root_interrupts(review)[0].value["kind"] == "plan_review"
    plan = PlanState.model_validate(_root_values(review)[-1]["tinkerfin_plan"])
    assert plan.revision == 1
    answer = plan.clarification_history[0].answers[0]
    assert answer.option_id == "staging"
    assert answer.answer == "Staging"


@pytest.mark.asyncio
async def test_ag_ui_planner_clarification_preserves_options_and_resume_path() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(
                responses=[
                    _gate("plan"),
                    _planner_clarification(),
                    _planner(),
                ]
            ),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    clarification_events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="agui-planner-clarify-1",
        config=config,
    )

    terminal = _terminal(clarification_events)
    state_snapshots = [
        event for event in clarification_events if isinstance(event, StateSnapshotEvent)
    ]
    assert state_snapshots
    assert all(
        "_tinkerfin_plan_clarification_schema" not in event.snapshot
        for event in state_snapshots
    )
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    interrupt = terminal.outcome.interrupts[0]
    assert interrupt.reason == "plan_clarification"
    metadata = cast(Mapping[str, object], interrupt.metadata)
    correlation = cast(Mapping[str, object], metadata["runtimeInterrupt"])
    envelope = cast(Mapping[str, object], correlation["envelope"])
    plan_metadata = cast(Mapping[str, object], envelope["metadata"])
    assert plan_metadata["source"] == "planner"
    clarification_metadata = cast(Mapping[str, object], plan_metadata["clarification"])
    assert clarification_metadata["schema"] == "tinkerfin.plan-clarification.v1"
    assert "schemaFingerprint" not in clarification_metadata
    form = cast(Mapping[str, object], clarification_metadata["form"])
    questions = cast(Sequence[Mapping[str, object]], form["questions"])
    assert questions[0]["allowFreeText"] is False
    assert questions[0]["options"] == [
        {
            "id": "staging",
            "label": "Staging",
            "description": "Use the test environment",
            "attributes": None,
        },
        {
            "id": "production",
            "label": "Production",
            "description": "Use the live environment",
            "attributes": None,
        },
    ]
    event_types = [event.type.value for event in clarification_events]
    state_index = len(event_types) - 1 - event_types[::-1].index("STATE_SNAPSHOT")
    messages_index = len(event_types) - 1 - event_types[::-1].index("MESSAGES_SNAPSHOT")
    terminal_index = len(event_types) - 1 - event_types[::-1].index("RUN_FINISHED")
    assert state_index < messages_index < terminal_index

    translation = ResumeMapper().map_agui(
        entries=(
            _resume_entry(
                interrupt.id,
                {
                    "type": "respond",
                    "answers": [
                        {
                            "questionId": "planner-q-1",
                            "optionId": "staging",
                        }
                    ],
                },
            ),
        ),
        interrupts=terminal.outcome.interrupts,
    )
    resume_identity = _identity("agui-planner-clarify-2")
    binding = AgUiResumeBinding.from_translation(
        identity=resume_identity,
        translation=translation,
    )
    review_events = await _agui_events(
        definition,
        binding.command,
        run_id=resume_identity.run_id,
        config=config,
        resume=binding,
    )

    review_terminal = _terminal(review_events)
    assert review_terminal.outcome is not None
    assert review_terminal.outcome.type == "interrupt"
    assert review_terminal.outcome.interrupts[0].reason == "plan_review"


@pytest.mark.asyncio
async def test_plan_edit_creates_a_new_revision_before_reviewing_again() -> None:
    model = _FakeModel(responses=[_gate("plan"), _planner()])
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="edit-1",
        config=config,
    )
    edited = await _parts(
        definition,
        Command(
            resume={
                "type": "edit",
                "baseRevision": 1,
                "draft": {
                    "goal": "Edited goal",
                    "assumptions": [],
                    "steps": [
                        {
                            "id": "step-1",
                            "title": "Edited",
                            "description": "Use edited implementation",
                            "verification": ["Edited test passes"],
                        }
                    ],
                    "acceptanceCriteria": ["Edited result works"],
                },
            }
        ),
        run_id="edit-2",
        config=config,
    )

    assert _root_interrupts(edited)[0].value["metadata"]["planRevision"] == 2
    plan = PlanState.model_validate(_root_values(edited)[-1]["tinkerfin_plan"])
    assert plan.revision == 2
    assert plan.draft is not None and plan.draft.goal == "Edited goal"


@pytest.mark.asyncio
async def test_plan_response_feedback_produces_a_revised_draft() -> None:
    model = _FakeModel(
        responses=[
            _gate("plan"),
            _planner(),
            _planner(suffix=" revised"),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="respond-1",
        config=config,
    )

    revised = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "baseRevision": 1,
                "message": "Add a regression check",
            }
        ),
        run_id="respond-2",
        config=config,
    )

    assert _root_interrupts(revised)[0].value["metadata"]["planRevision"] == 2
    plan = PlanState.model_validate(_root_values(revised)[-1]["tinkerfin_plan"])
    assert plan.revision == 2
    assert plan.feedback == ("Add a regression check",)
    assert plan.draft is not None and plan.draft.goal.endswith("revised")


@pytest.mark.asyncio
async def test_plan_review_rejects_a_stale_base_revision() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(responses=[_gate("plan"), _planner()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="stale-1",
        config=config,
    )

    with pytest.raises(ValueError, match="baseRevision is stale"):
        await _parts(
            definition,
            Command(resume={"type": "approve", "baseRevision": 2}),
            run_id="stale-2",
            config=config,
        )


@pytest.mark.asyncio
async def test_duplicate_plan_resume_does_not_execute_the_agent_again() -> None:
    model = _FakeModel(responses=[_gate("plan"), _planner(), AIMessage(content="done")])
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="duplicate-1",
        config=config,
    )
    command = Command(resume={"type": "approve", "baseRevision": 1})
    await _parts(
        definition,
        command,
        run_id="duplicate-2",
        config=config,
    )
    calls_after_completion = len(model.model_inputs)

    duplicate = await _parts(
        definition,
        command,
        run_id="duplicate-3",
        config=config,
    )

    assert len(model.model_inputs) == calls_after_completion
    assert len(duplicate) == 1
    plan = PlanState.model_validate(_root_values(duplicate)[-1]["tinkerfin_plan"])
    assert plan.status.value == "completed"


@pytest.mark.asyncio
async def test_plan_reject_ends_without_running_the_deep_agent() -> None:
    model = _FakeModel(responses=[_gate("plan"), _planner()])
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="reject-1",
        config=config,
    )
    rejected = await _parts(
        definition,
        Command(resume={"type": "reject", "baseRevision": 1}),
        run_id="reject-2",
        config=config,
    )

    plan = PlanState.model_validate(_root_values(rejected)[-1]["tinkerfin_plan"])
    assert plan.status.value == "cancelled"
    assert not any(
        part["type"] == "tasks"
        and cast(Mapping[str, object], part["data"]).get("name") == "execute_deep_agent"
        for part in rejected
    )


@pytest.mark.asyncio
async def test_plan_mode_rejects_non_sync_durability() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(responses=[_gate("direct")]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )

    with pytest.raises(PlanModeConfigurationError, match="durability='sync'"):
        await _parts(
            definition,
            {"messages": [HumanMessage(content="Implement")]},
            run_id="bad-durability",
            config={"configurable": {"thread_id": "plan-thread"}},
            durability="async",
        )


@tool
def approved_tool(value: str) -> str:
    """Return a value after human approval."""

    return value


@tool
def read_stored_value(runtime: ToolRuntime[None, object]) -> str:
    """Read a value from the inherited LangGraph Store."""

    store = runtime.store
    if store is None:
        raise RuntimeError("Store was not inherited by the execution subgraph")
    item = store.get(("plan-mode",), "value")
    if item is None:
        raise RuntimeError("Stored value is missing")
    value = item.value.get("text")
    if not isinstance(value, str):
        raise TypeError("Stored text must be a string")
    return value


@pytest.mark.asyncio
async def test_pending_tool_interrupt_is_independent_from_the_next_bound_mode() -> None:
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_tool",
                        "args": {"value": "ok"},
                        "id": "default-tool-call",
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

    pending = await _parts(
        definition,
        {"messages": [HumanMessage(content="Use the approved tool")]},
        run_id="tool-mode-1",
        config=config,
        mode="default",
    )
    assert "action_requests" in _root_interrupts(pending)[0].value

    completed = await _parts(
        definition,
        Command(resume={"decisions": [{"type": "approve"}]}),
        run_id="tool-mode-2",
        config=config,
        mode="plan",
    )

    assert _root_interrupts(completed) == ()
    final = _root_values(completed)[-1]
    assert PlanState.model_validate(final["tinkerfin_plan"]).status.value == "completed"
    assert len(model.model_inputs) == 2


@pytest.mark.asyncio
async def test_plan_and_deep_agent_tool_interrupts_resume_at_the_parent() -> None:
    model = _FakeModel(
        responses=[
            _gate("plan"),
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
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[approved_tool],
            interrupt_on={"approved_tool": {"allowed_decisions": ["approve"]}},
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="double-1",
        config=config,
    )
    tool_review = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="double-2",
        config=config,
    )
    tool_interrupt = _root_interrupts(tool_review)[0]
    assert "action_requests" in tool_interrupt.value

    completed = await _parts(
        definition,
        Command(resume={"decisions": [{"type": "approve"}]}),
        run_id="double-3",
        config=config,
    )
    plan = PlanState.model_validate(_root_values(completed)[-1]["tinkerfin_plan"])
    assert plan.status.value == "completed"


@pytest.mark.asyncio
async def test_execution_subgraph_inherits_the_parent_store() -> None:
    model = _FakeModel(
        responses=[
            _gate("plan"),
            _planner(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_stored_value",
                        "args": {},
                        "id": "store-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="store complete"),
        ]
    )
    store = InMemoryStore()
    store.put(("plan-mode",), "value", {"text": "inherited"})
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=model,
            tools=[read_stored_value],
            checkpointer=InMemorySaver(),
            store=store,
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="store-1",
        config=config,
    )

    completed = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 1}),
        run_id="store-2",
        config=config,
    )

    final = _root_values(completed)[-1]
    messages = cast(Sequence[object], final["messages"])
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "store-call"
        and message.content == "inherited"
        for message in messages
    )
    assert PlanState.model_validate(final["tinkerfin_plan"]).status.value == "completed"


@pytest.mark.asyncio
async def test_ag_ui_plan_review_resumes_to_one_success_terminal() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(
                responses=[_gate("plan"), _planner(), AIMessage(content="done")]
            ),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review_events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="agui-plan-1",
        config=config,
    )
    review_terminal = _terminal(review_events)
    assert review_terminal.outcome is not None
    assert review_terminal.outcome.type == "interrupt"
    interrupt = review_terminal.outcome.interrupts[0]
    assert interrupt.reason == "plan_review"
    assert interrupt.tool_call_id is None
    event_types = [event.type.value for event in review_events]
    state_index = len(event_types) - 1 - event_types[::-1].index("STATE_SNAPSHOT")
    messages_index = len(event_types) - 1 - event_types[::-1].index("MESSAGES_SNAPSHOT")
    terminal_index = len(event_types) - 1 - event_types[::-1].index("RUN_FINISHED")
    assert state_index < messages_index < terminal_index

    translation = ResumeMapper().map_agui(
        entries=(
            _resume_entry(
                interrupt.id,
                {"type": "approve", "baseRevision": 1},
            ),
        ),
        interrupts=review_terminal.outcome.interrupts,
    )
    resume_identity = _identity("agui-plan-2")
    binding = AgUiResumeBinding.from_translation(
        identity=resume_identity,
        translation=translation,
    )
    completed_events = await _agui_events(
        definition,
        binding.command,
        run_id=resume_identity.run_id,
        config=config,
        resume=binding,
    )

    completed_terminal = _terminal(completed_events)
    assert completed_terminal.outcome is not None
    assert completed_terminal.outcome.type == "success"
    snapshots = [
        event for event in completed_events if isinstance(event, StateSnapshotEvent)
    ]
    assert (
        PlanState.model_validate(snapshots[0].snapshot["tinkerfin_plan"]).status.value
        == "awaiting_review"
    )
    deltas = [
        event.delta for event in completed_events if isinstance(event, StateDeltaEvent)
    ]
    assert {
        "op": "replace",
        "path": "/tinkerfin_plan/status",
        "value": "completed",
    } in deltas[-1]


@pytest.mark.asyncio
async def test_ag_ui_execution_error_emits_one_error_terminal() -> None:
    @wrap_model_call
    def fail_execution(_request: Any, _handler: Any) -> Any:
        raise RuntimeError("execution failed")

    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(responses=[_gate("plan"), _planner()]),
            tools=[],
            middleware=[fail_execution],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review_events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="agui-error-1",
        config=config,
    )
    review_terminal = _terminal(review_events)
    assert review_terminal.outcome is not None
    assert review_terminal.outcome.type == "interrupt"
    interrupt = review_terminal.outcome.interrupts[0]
    translation = ResumeMapper().map_agui(
        entries=(
            _resume_entry(
                interrupt.id,
                {"type": "approve", "baseRevision": 1},
            ),
        ),
        interrupts=review_terminal.outcome.interrupts,
    )
    identity = _identity("agui-error-2")
    binding = AgUiResumeBinding.from_translation(
        identity=identity,
        translation=translation,
    )

    error_events = await _agui_events(
        definition,
        binding.command,
        run_id=identity.run_id,
        config=config,
        resume=binding,
    )

    event_types = [event.type.value for event in error_events]
    assert event_types[0] == "RUN_STARTED"
    assert event_types[-1] == "RUN_ERROR"
    assert event_types.count("RUN_ERROR") == 1
    assert "RUN_FINISHED" not in event_types


@pytest.mark.asyncio
async def test_cancellation_propagates_into_the_execution_subgraph() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    @wrap_model_call
    async def block_execution(_request: Any, _handler: Any) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(responses=[_gate("plan"), _planner()]),
            tools=[],
            middleware=[block_execution],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="cancel-1",
        config=config,
    )
    runtime = cast(Any, definition).new(identity=_identity("cancel-2"))
    stream = runtime.astream(
        Command(resume={"type": "approve", "baseRevision": 1}),
        config=config,
        stream_mode=["messages", "tasks", "values"],
        subgraphs=True,
    )

    async def consume() -> list[object]:
        return [part async for part in stream]

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=2)
    consumer.cancel("request disconnected")
    with pytest.raises(asyncio.CancelledError, match="request disconnected"):
        await consumer
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    await stream.aclose()


@pytest.mark.asyncio
async def test_ag_ui_tool_review_keeps_the_scoped_tool_id_after_plan_approval() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, default_mode="plan")
        .create_deep_agent(
            model=_FakeModel(
                responses=[
                    _gate("plan"),
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
            ),
            tools=[approved_tool],
            interrupt_on={"approved_tool": {"allowed_decisions": ["approve"]}},
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    plan_events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement")]},
        run_id="agui-tool-1",
        config=config,
    )
    plan_terminal = _terminal(plan_events)
    assert plan_terminal.outcome is not None
    assert plan_terminal.outcome.type == "interrupt"
    plan_interrupt = plan_terminal.outcome.interrupts[0]
    plan_translation = ResumeMapper().map_agui(
        entries=(
            _resume_entry(
                plan_interrupt.id,
                {"type": "approve", "baseRevision": 1},
            ),
        ),
        interrupts=plan_terminal.outcome.interrupts,
    )
    tool_identity = _identity("agui-tool-2")
    plan_binding = AgUiResumeBinding.from_translation(
        identity=tool_identity,
        translation=plan_translation,
    )

    tool_events = await _agui_events(
        definition,
        plan_binding.command,
        run_id=tool_identity.run_id,
        config=config,
        resume=plan_binding,
    )
    tool_terminal = _terminal(tool_events)
    assert tool_terminal.outcome is not None
    assert tool_terminal.outcome.type == "interrupt"
    tool_interrupt = tool_terminal.outcome.interrupts[0]
    assert tool_interrupt.reason == "tool_call"
    assert tool_interrupt.tool_call_id is not None
    scoped_tool_id = tool_interrupt.tool_call_id
    assert any(
        isinstance(event, ToolCallStartEvent) and event.tool_call_id == scoped_tool_id
        for event in tool_events
    )

    tool_translation = ResumeMapper().map_agui(
        entries=(_resume_entry(tool_interrupt.id, {"type": "approve"}),),
        interrupts=tool_terminal.outcome.interrupts,
    )
    completed_identity = _identity("agui-tool-3")
    tool_binding = AgUiResumeBinding.from_translation(
        identity=completed_identity,
        translation=tool_translation,
    )
    completed_events = await _agui_events(
        definition,
        tool_binding.command,
        run_id=completed_identity.run_id,
        config=config,
        resume=tool_binding,
    )

    completed_terminal = _terminal(completed_events)
    assert completed_terminal.outcome is not None
    assert completed_terminal.outcome.type == "success"
    assert any(
        isinstance(event, ToolCallResultEvent) and event.tool_call_id == scoped_tool_id
        for event in completed_events
    )
    assert not any(
        isinstance(event, ToolCallStartEvent) and event.tool_call_id == scoped_tool_id
        for event in completed_events
    )
