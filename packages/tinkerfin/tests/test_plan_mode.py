"""Public Plan Mode contracts and native Deep Agent handoff behavior."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypedDict, cast
from uuid import uuid4

import pytest
from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
)
from ag_ui.core.types import ResumeEntry
from deepagents import create_deep_agent
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from deepagents.graph import DeepAgentState
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.types import AgentMiddleware, AgentState
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
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Interrupt, interrupt
from pydantic import Field, PrivateAttr, ValidationError, field_serializer
from redis.asyncio import Redis
from redis.exceptions import ResponseError

import tinkerfin.plan as plan_api
from tinkerfin import AgUiResumeBinding, AgUiResumeCheckpoint, Identity, TinkerFin
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationFormBase,
    ClarificationModel,
    ClarificationOption,
    ClarificationQuestion,
    DefaultClarificationForm,
    MarkdownPlanContent,
    PlanContentModel,
    PlanDraft,
    PlanHandoffPhase,
    PlanModeConfigurationError,
    PlanSchemaReference,
    PlanState,
    PlanStatus,
    PlanStructuredOutputError,
    StructuredPlanContent,
    StructuredPlanStep,
)
from tinkerfin.plan._workflow import PlanningWorkflowGraph
from tinkerfin_agui_adapter import (
    ResumeMapper,
    RuntimeInterruptEnvelope,
    ScopedIdCodec,
)


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


class _BoundedForm(ClarificationForm[_CustomQuestion]):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=2, max_length=5)


class _ExactFourForm(ClarificationForm[_CustomQuestion]):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=4, max_length=4)


class _MissingMinimumForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...]


class _DefaultedQuestionsForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...] = Field(default=(), min_length=1)


class _AliasedQuestionsForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=1, alias="items")


class _ImpossibleRangeForm(ClarificationFormBase):
    questions: tuple[_CustomQuestion, ...] = Field(min_length=2, max_length=1)


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


class _CustomPlanContent(PlanContentModel):
    schema_id = "example.release-plan.v1"

    summary: str
    checks: tuple[str, ...]


class _MissingPlanSchemaId(PlanContentModel):
    value: str


class _MarkdownImpostor(PlanContentModel):
    schema_id = "example.markdown-impostor.v1"
    media_type = "text/markdown"

    markdown: str


def _identity(run_id: str, *, thread_id: str = "plan-thread") -> Identity:
    return Identity(threadId=thread_id, runId=run_id)


def _structured_plan_state(value: object) -> PlanState[StructuredPlanContent]:
    return PlanState[StructuredPlanContent].model_validate(value)


def _markdown_plan_state(value: object) -> PlanState[MarkdownPlanContent]:
    return PlanState[MarkdownPlanContent].model_validate(value)


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


def _markdown_planner(markdown: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "clarification": None,
                    "draft": {"markdown": markdown},
                },
                "id": "markdown-planner",
                "type": "tool_call",
            }
        ],
        id="markdown-planner-message",
    )


def _custom_plan_planner() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "draft",
                    "clarification": None,
                    "draft": {
                        "summary": "Release safely",
                        "checks": ["Targeted tests pass"],
                    },
                },
                "id": "custom-plan-planner",
                "type": "tool_call",
            }
        ],
        id="custom-plan-planner-message",
    )


def _planner_clarification(
    *,
    question_id: str = "target",
    prompt: str = "Which target?",
    options: list[dict[str, object]] | None = None,
    required: bool = True,
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
                                "required": required,
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


def _planner_clarification_batch(
    count: int = 4,
    *,
    required: bool = True,
) -> AIMessage:
    questions = [
        {
            "id": f"question-{index}",
            "prompt": f"Choose value {index}",
            "required": required,
            "options": (
                [{"id": f"option-{index}", "label": f"Option {index}"}]
                if index % 2 == 0
                else []
            ),
            "allow_free_text": True,
        }
        for index in range(count)
    ]
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "PlannerOutcome",
                "args": {
                    "type": "clarify",
                    "draft": None,
                    "clarification": {"questions": questions},
                },
                "id": "clarify-batch",
                "type": "tool_call",
            }
        ],
        id="clarify-batch-message",
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
                                "required": True,
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
                                "required": True,
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
    thread_id: str = "plan-thread",
    mode: str | None = None,
    resume: AgUiResumeBinding | None = None,
    parent_run_id: str | None = None,
) -> list[BaseEvent]:
    identity = _identity(run_id, thread_id=thread_id)
    runtime = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id=parent_run_id,
        mode=mode,
        resume=resume,
    )
    stream = (
        runtime.astream(config=config)
        if resume is not None
        else runtime.astream(graph_input, config=config)
    )
    return [event async for event in stream]


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
    payload: Mapping[str, object],
) -> AgUiResumeBinding:
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    interrupt = terminal.outcome.interrupts[0]
    return AgUiResumeBinding.from_agui(
        entries=(_resume_entry(interrupt.id, payload),),
        interrupts=terminal.outcome.interrupts,
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


def test_plan_package_exports_only_the_v1_contract() -> None:
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
        "MarkdownPlanContent",
        "PendingClarification",
        "PlanContentModel",
        "PlanDraft",
        "PlanHandoff",
        "PlanHandoffPhase",
        "PlanModeConfigurationError",
        "PlanReviewAction",
        "PlanSchemaReference",
        "PlanState",
        "PlanStateConflictError",
        "PlanStatus",
        "PlanStructuredOutputError",
        "RequirementAnswer",
        "StructuredPlanContent",
        "StructuredPlanStep",
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


def test_plan_configuration_defaults_to_structured_and_freezes_custom_content() -> None:
    default_options = TinkerFin().plan()._plan_options
    custom_options = TinkerFin().plan(plan_schema=_CustomPlanContent)._plan_options

    assert default_options is not None
    assert default_options.content.schema is StructuredPlanContent
    assert default_options.content.reference.id == "tinkerfin.plan.structured.v1"
    assert custom_options is not None
    assert custom_options.content.schema is _CustomPlanContent
    assert custom_options.content.reference.id == "example.release-plan.v1"
    planner_schema = custom_options.contracts.planner_response_type.model_json_schema(
        by_alias=True
    )
    assert "_CustomPlanContent" in planner_schema["$defs"]
    review_schema = custom_options.contracts.review_response.json_schema(by_alias=True)
    assert review_schema["discriminator"]["mapping"]["edit"].endswith("/EditPlan")
    assert review_schema["$defs"]["EditPlan"]["properties"]["content"] == {
        "$ref": "#/$defs/_CustomPlanContent"
    }


@pytest.mark.parametrize(
    "schema",
    [object, PlanContentModel, _MissingPlanSchemaId, _MarkdownImpostor],
)
def test_plan_configuration_rejects_invalid_content_schemas(schema: object) -> None:
    with pytest.raises(PlanModeConfigurationError):
        TinkerFin().plan(plan_schema=cast(Any, schema))


def test_disabled_plan_rejects_a_non_default_content_schema() -> None:
    with pytest.raises(PlanModeConfigurationError, match="disabled Plan capability"):
        TinkerFin().plan(enabled=False, plan_schema=MarkdownPlanContent)


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (_MissingMinimumForm, "minItems >= 1"),
        (_DefaultedQuestionsForm, "must be required"),
        (_AliasedQuestionsForm, "stable JSON array field"),
        (_ImpossibleRangeForm, "maxItems must be >= minItems"),
    ],
)
def test_plan_configuration_rejects_unsafe_question_count_schemas(
    schema: type[ClarificationFormBase],
    message: str,
) -> None:
    with pytest.raises(PlanModeConfigurationError, match=message):
        TinkerFin().plan(clarification_schema=schema)


def test_plan_contracts_are_frozen_and_reject_duplicate_steps() -> None:
    step = StructuredPlanStep(
        id="step-1",
        title="Implement",
        description="Implement feature",
        verification=("Tests pass",),
    )
    content = StructuredPlanContent(
        goal="Implement feature",
        steps=(step,),
        acceptance_criteria=("Feature works",),
    )
    draft = PlanDraft[StructuredPlanContent](
        revision=1,
        content_schema=PlanSchemaReference(
            id=StructuredPlanContent.schema_id or "",
            fingerprint="0" * 64,
            media_type=StructuredPlanContent.media_type,
        ),
        content=content,
    )
    with pytest.raises(ValidationError):
        draft.revision = 2  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError, match="step IDs must be unique"):
        StructuredPlanContent(
            goal="Implement",
            steps=(step, step),
            acceptance_criteria=("Works",),
        )
    assert PlanState().workflow_version == "tinkerfin.plan.v1"


def test_markdown_plan_rejects_blank_text_without_rewriting_content() -> None:
    markdown = "  # Plan\r\n\r\n```python\r\nprint('ok')\r\n```\r\n"

    assert MarkdownPlanContent(markdown=markdown).markdown == markdown
    with pytest.raises(ValidationError, match="markdown must not be blank"):
        MarkdownPlanContent(markdown=" \n\t")


def test_default_clarification_form_is_non_empty_without_an_upper_limit() -> None:
    schema = DefaultClarificationForm.model_json_schema(by_alias=True)
    assert schema["properties"]["schemaVersion"]["const"] == 2
    questions_schema = schema["properties"]["questions"]
    assert questions_schema["minItems"] == 1
    assert "maxItems" not in questions_schema

    for count in (1, 4, 8):
        form = DefaultClarificationForm.model_validate(
            {
                "questions": [
                    {
                        "id": f"question-{index}",
                        "prompt": "Choose",
                        "required": True,
                    }
                    for index in range(count)
                ]
            }
        )
        assert len(form.questions) == count
        assert all(question.required for question in form.questions)
    with pytest.raises(ValidationError, match="required"):
        DefaultClarificationForm.model_validate(
            {"questions": [{"id": "missing-required", "prompt": "Choose"}]}
        )
    with pytest.raises(ValidationError, match="at least 1 item"):
        DefaultClarificationForm.model_validate({"questions": []})
    with pytest.raises(ValidationError, match="at least one question"):
        _MissingMinimumForm.model_validate({"questions": []})


def test_host_schema_owns_question_count_bounds() -> None:
    bounded_schema = _BoundedForm.model_json_schema(by_alias=True)["properties"][
        "questions"
    ]
    assert bounded_schema["minItems"] == 2
    assert bounded_schema["maxItems"] == 5
    TinkerFin().plan(clarification_schema=_BoundedForm)

    def payload(count: int) -> dict[str, list[dict[str, object]]]:
        return {
            "questions": [
                {
                    "id": f"question-{index}",
                    "prompt": "Choose",
                    "required": True,
                }
                for index in range(count)
            ]
        }

    assert len(_BoundedForm.model_validate(payload(2)).questions) == 2
    assert len(_BoundedForm.model_validate(payload(5)).questions) == 5
    with pytest.raises(ValidationError, match="at least 2 items"):
        _BoundedForm.model_validate(payload(1))
    with pytest.raises(ValidationError, match="at most 5 items"):
        _BoundedForm.model_validate(payload(6))

    TinkerFin().plan(clarification_schema=_ExactFourForm)
    assert len(_ExactFourForm.model_validate(payload(4)).questions) == 4
    with pytest.raises(ValidationError):
        _ExactFourForm.model_validate(payload(3))
    with pytest.raises(ValidationError):
        _ExactFourForm.model_validate(payload(5))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            DefaultClarificationForm,
            "requires at least 1 question and sets no maximum",
        ),
        (_BoundedForm, "requires between 2 and 5 questions, inclusive"),
        (_ExactFourForm, "requires exactly 4 questions"),
    ],
)
async def test_planner_prompt_derives_question_count_from_the_bound_schema(
    schema: type[ClarificationFormBase],
    expected: str,
) -> None:
    model = _FakeModel(responses=[_planner()])
    definition = (
        TinkerFin()
        .plan(enabled=True, clarification_schema=schema)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="dynamic-count-message")]},
        run_id="dynamic-count",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )

    system_message = next(
        message
        for message in model.model_inputs[0]
        if isinstance(message, SystemMessage)
    )
    assert expected in str(system_message.content)


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
    plan = _structured_plan_state(_root_values(parts)[-1]["tinkerfin_plan"])
    assert interrupt.value["kind"] == "tinkerfin:plan_review"
    review = interrupt.value["metadata"]["review"]
    assert review["schema"] == "tinkerfin.plan-review.v1"
    assert review["draft"]["revision"] == 1
    assert review["draft"]["contentSchema"]["id"] == ("tinkerfin.plan.structured.v1")
    assert review["draft"]["content"]["goal"] == "Implement feature"
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
async def test_markdown_plan_review_edit_and_handoff_preserve_exact_text() -> None:
    initial = "  # Release Plan\r\n\r\n```sh\r\necho ready\r\n```\r\n"
    edited = f"{initial}\r\n- Browser regression\r\n"
    model = _FakeModel(
        responses=[
            _markdown_planner(initial),
            _accept_edit(),
            AIMessage(content="native done"),
        ]
    )
    definition = (
        TinkerFin()
        .plan(enabled=True, plan_schema=MarkdownPlanContent)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan release", id="markdown-request")]},
        run_id="markdown-1",
        config=config,
        mode="plan",
    )
    first_interrupt = _root_interrupts(first)[0]
    first_review = first_interrupt.value["metadata"]["review"]
    assert first_review["schema"] == "tinkerfin.plan-review.v1"
    assert first_review["draft"]["contentSchema"]["mediaType"] == "text/markdown"
    assert first_review["draft"]["content"]["markdown"] == initial

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "edit",
                "baseRevision": 1,
                "content": {"markdown": edited},
            }
        ),
        run_id="markdown-2",
        config=config,
        mode="plan",
    )
    second_interrupt = _root_interrupts(second)[0]
    second_review = second_interrupt.value["metadata"]["review"]
    assert second_review["draft"]["revision"] == 2
    assert second_review["draft"]["content"]["markdown"] == edited
    pending = _markdown_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert pending.draft is not None
    assert pending.draft.content.markdown == edited

    completed = await _parts(
        definition,
        Command(resume={"type": "approve", "baseRevision": 2}),
        run_id="markdown-3",
        config=config,
        mode="default",
    )
    final = _root_values(completed)[-1]
    plan = _markdown_plan_state(final["tinkerfin_plan"])
    assert plan.confirmed_plan is not None
    assert plan.confirmed_plan.content.markdown == edited
    execution_input = model.model_inputs[-1]
    handoff = next(
        message
        for message in execution_input
        if isinstance(message, HumanMessage) and message.id == "markdown-request"
    )
    handoff_text = str(handoff.content)
    assert 'content-type="text/markdown"' in handoff_text
    assert edited in handoff_text
    assert "\\r\\n" not in handoff_text


@pytest.mark.asyncio
async def test_custom_plan_schema_round_trips_through_review_state() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True, plan_schema=_CustomPlanContent)
        .create_deep_agent(
            model=_FakeModel(responses=[_custom_plan_planner()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    parts = await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan release", id="custom-request")]},
        run_id="custom-plan",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    review = _root_interrupts(parts)[0].value["metadata"]["review"]
    assert review["draft"]["contentSchema"]["id"] == "example.release-plan.v1"
    assert review["draft"]["content"] == {
        "summary": "Release safely",
        "checks": ["Targeted tests pass"],
    }
    state = PlanState[_CustomPlanContent].model_validate(
        _root_values(parts)[-1]["tinkerfin_plan"]
    )
    assert state.draft is not None
    assert state.draft.content.summary == "Release safely"


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
    assert _root_interrupts(parts)[0].value["kind"] == "tinkerfin:plan_review"
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
    plan = _structured_plan_state(final["tinkerfin_plan"])
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
    assert _root_interrupts(first)[0].value["kind"] == "tinkerfin:plan_review"


@pytest.mark.asyncio
@pytest.mark.redis_e2e
async def test_real_redis_plan_approval_executes_native_handoff() -> None:
    redis_url = os.getenv("TINKERFIN_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("real Redis configuration is missing: TINKERFIN_TEST_REDIS_URL")

    token = uuid4().hex
    thread_id = f"tinkerfin-plan-handoff-{token}"
    checkpoint_prefix = f"tinkerfin:test:plan:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:plan:{token}:write"
    client = Redis.from_url(
        redis_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    try:
        await saver.asetup()
        model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
        definition = (
            TinkerFin()
            .plan(enabled=True)
            .create_deep_agent(model=model, tools=[], checkpointer=saver)
        )
        config = {"configurable": {"thread_id": thread_id}}
        review = await _agui_events(
            definition,
            {"messages": [HumanMessage(content="Implement", id="request-redis")]},
            run_id="redis-review",
            config=config,
            thread_id=thread_id,
            mode="plan",
        )
        binding = _plan_binding(
            _terminal(review),
            payload={"type": "approve", "baseRevision": 1},
        )
        completed = await _agui_events(
            definition,
            None,
            run_id="redis-execution",
            config=config,
            thread_id=thread_id,
            mode="default",
            resume=binding,
        )

        assert _terminal(completed).outcome.type == "success"
        assert len(model.model_inputs) == 2
        assert any(
            getattr(event, "delta", None) == "native done" for event in completed
        )
        execution_input = model.model_inputs[-1]
        assert any(
            isinstance(message, HumanMessage)
            and message.id == "request-redis"
            and "<tinkerfin-approved-plan" in str(message.content)
            for message in execution_input
        )
    finally:
        try:
            await saver.adelete_thread(thread_id)
        finally:
            registry_keys = [
                key
                async for key in client.scan_iter(
                    match=f"write_keys_zset:{thread_id}:*",
                    count=100,
                )
            ]
            latest_keys = [
                key
                async for key in client.scan_iter(
                    match=f"{checkpoint_prefix}_latest:{thread_id}:*",
                    count=100,
                )
            ]
            if registry_keys or latest_keys:
                await client.unlink(*registry_keys, *latest_keys)
            for index_name in (checkpoint_prefix, write_prefix):
                try:
                    await client.execute_command("FT.DROPINDEX", index_name, "DD")
                except ResponseError:
                    pass
            await client.aclose()


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
        _structured_plan_state(_root_values(duplicate)[0]["tinkerfin_plan"]).status
        is PlanStatus.APPROVED
    )


@pytest.mark.asyncio
async def test_plan_handoff_recovers_after_native_checkpoint_precedes_plan_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry must continue a durably accepted handoff without losing execution."""

    model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
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
        run_id="handoff-crash-1",
        config=config,
        mode="plan",
    )
    original = PlanningWorkflowGraph.mark_handoff_phase

    async def cancel_after_native_checkpoint(
        self: PlanningWorkflowGraph[Any],
        *args: object,
        **kwargs: object,
    ) -> PlanState[PlanContentModel]:
        phase = kwargs.get("phase")
        if phase is PlanHandoffPhase.ACCEPTED:
            raise asyncio.CancelledError
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(
        PlanningWorkflowGraph,
        "mark_handoff_phase",
        cancel_after_native_checkpoint,
    )
    command = Command(resume={"type": "approve", "baseRevision": 1})
    with pytest.raises(asyncio.CancelledError):
        await _parts(
            definition,
            command,
            run_id="handoff-crash-2",
            config=config,
            mode="default",
        )
    monkeypatch.setattr(PlanningWorkflowGraph, "mark_handoff_phase", original)

    retry = await _parts(
        definition,
        command,
        run_id="handoff-crash-3",
        config=config,
        mode="default",
    )
    final = _structured_plan_state(_root_values(retry)[-1]["tinkerfin_plan"])
    assert len(model.model_inputs) == 2
    assert final.handoff is not None
    assert final.handoff.phase is PlanHandoffPhase.COMPLETED
    assert any("native done" in str(part.get("data")) for part in retry)


@pytest.mark.asyncio
async def test_agui_plan_resume_checkpoints_durably_and_retries_without_reexecution() -> (
    None
):
    model = _FakeModel(responses=[_planner(), AIMessage(content="native done")])
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
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Implement", id="message")]},
        run_id="settlement-review",
        config=config,
        mode="plan",
    )
    terminal = _terminal(review)
    payload = {"type": "approve", "baseRevision": 1}
    binding = _plan_binding(
        terminal,
        payload=payload,
    )
    identity = _identity("settlement-execution")
    checkpoints: list[AgUiResumeCheckpoint] = []

    async def checkpointed(value: AgUiResumeCheckpoint) -> None:
        checkpoints.append(value)

    runtime = cast(Any, definition).new_agui(
        identity=identity,
        mode="default",
        resume=binding,
        on_resume_checkpointed=checkpointed,
    )
    events = [
        event
        async for event in runtime.astream(
            config=config,
        )
    ]
    calls = len(model.model_inputs)

    assert _terminal(events).outcome.type == "success"
    assert len(checkpoints) == 1
    assert all(
        "_tinkerfin_resume" not in event.snapshot
        and "_tinkerfin_lineage" not in event.snapshot
        for event in events
        if isinstance(event, StateSnapshotEvent)
    )

    retry = cast(Any, definition).new_agui(
        identity=identity,
        mode="default",
        resume=binding,
        on_resume_checkpointed=checkpointed,
    )
    retried = [
        event
        async for event in retry.astream(
            config=config,
        )
    ]

    assert _terminal(retried).outcome.type == "success"
    assert len(model.model_inputs) == calls
    assert len(checkpoints) == 2
    assert checkpoints[0] == checkpoints[1]


@pytest.mark.asyncio
async def test_plan_capable_runtime_routes_plan_shaped_generic_resume_to_native() -> (
    None
):
    resumed_payloads: list[object] = []
    envelope = RuntimeInterruptEnvelope(
        kind="vendor:approval",
        message="Review vendor action",
        response_schema={
            "type": "object",
            "properties": {
                "type": {"const": "approve"},
                "baseRevision": {"type": "integer"},
                "vendor": {"type": "string"},
            },
            "required": ["type", "baseRevision", "vendor"],
            "additionalProperties": False,
        },
        metadata={"source": "generic-native-fixture"},
    )

    class _GenericNativeInterrupt(AgentMiddleware):
        def after_model(
            self,
            state: AgentState[Any],
            runtime: Runtime[None],
        ) -> dict[str, object] | None:
            del state, runtime
            resumed_payloads.append(
                interrupt(
                    envelope.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                )
            )
            return None

    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[AIMessage(content="native done")]),
            tools=[],
            middleware=(_GenericNativeInterrupt(),),
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Run native", id="message")]},
        run_id="generic-review",
        config=config,
        mode="default",
    )
    terminal = _terminal(review)
    assert terminal.outcome.type == "interrupt"
    public_interrupt = terminal.outcome.interrupts[0]
    payload = {"type": "approve", "baseRevision": 999, "vendor": "native"}
    entry = _resume_entry(public_interrupt.id, payload)
    translation = ResumeMapper().map_agui(
        entries=(entry,),
        interrupts=(public_interrupt,),
    )
    assert translation.kind == "runtime"
    identity = _identity("generic-resume")
    binding = AgUiResumeBinding.from_agui(
        entries=(entry,),
        interrupts=(public_interrupt,),
    )
    runtime = cast(Any, definition).new_agui(
        identity=identity,
        mode="plan",
        resume=binding,
    )
    resumed = [
        event
        async for event in runtime.astream(
            config=config,
        )
    ]

    assert _terminal(resumed).outcome.type == "success"
    assert resumed_payloads == [payload]


@pytest.mark.asyncio
async def test_parent_run_id_branches_from_completed_plan_lineage() -> None:
    model = _FakeModel(
        responses=[
            _planner(),
            AIMessage(content="planned execution"),
            AIMessage(content="head continuation"),
            AIMessage(content="branched continuation"),
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
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Plan", id="plan-user")]},
        run_id="plan-lineage-review",
        config=config,
        mode="plan",
    )
    binding = _plan_binding(
        _terminal(review),
        payload={"type": "approve", "baseRevision": 1},
    )
    await _agui_events(
        definition,
        None,
        run_id="plan-lineage-execution",
        config=config,
        mode="default",
        resume=binding,
    )
    await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Head", id="head-user")]},
        run_id="plan-lineage-head",
        config=config,
        mode="default",
    )
    branched = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Branch", id="branch-user")]},
        run_id="plan-lineage-branch",
        parent_run_id="plan-lineage-execution",
        config=config,
        mode="default",
    )

    assert _terminal(branched).outcome.type == "success"
    latest_input = model.model_inputs[-1]
    human_ids = {
        message.id for message in latest_input if isinstance(message, HumanMessage)
    }
    assert "branch-user" in human_ids
    assert "head-user" not in human_ids


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
        payload={"type": "approve", "baseRevision": 1},
    )
    executed = await _agui_events(
        definition,
        None,
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
    assert second_outcome.interrupts[0].reason == "tinkerfin:plan_review"
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
        _structured_plan_state(_root_values(first)[-1]["tinkerfin_plan"]).revision == 0
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
    plan2 = _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
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
    plan3 = _structured_plan_state(_root_values(third)[-1]["tinkerfin_plan"])
    assert plan3.revision == 1
    assert len(plan3.clarification_history) == 2
    assert _root_interrupts(third)[0].value["kind"] == "tinkerfin:plan_review"


@pytest.mark.asyncio
async def test_four_question_clarification_round_trips_in_one_batch() -> None:
    model = _FakeModel(responses=[_planner_clarification_batch(), _planner()])
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(serde=JsonPlusSerializer(pickle_fallback=False)),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="four-question-message")]},
        run_id="four-question-1",
        config=config,
        mode="plan",
    )

    assert {part["type"] for part in first} == {"messages", "tasks", "values"}
    interrupts = _root_interrupts(first)
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "tinkerfin:plan_clarification"
    metadata = interrupts[0].value["metadata"]
    form = metadata["clarification"]["form"]
    assert [question["id"] for question in form["questions"]] == [
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    ]
    pending = _structured_plan_state(_root_values(first)[-1]["tinkerfin_plan"])
    assert pending.status is PlanStatus.AWAITING_CLARIFICATION
    assert pending.pending_clarification is not None
    pending_questions = pending.pending_clarification.form["questions"]
    assert isinstance(pending_questions, list)
    assert len(pending_questions) == 4

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [
                    {"questionId": "question-0", "optionId": "option-0"},
                    {"questionId": "question-1", "answer": "Custom 1"},
                    {"questionId": "question-2", "optionId": "option-2"},
                    {"questionId": "question-3", "answer": "Custom 3"},
                ],
            }
        ),
        run_id="four-question-2",
        config=config,
        mode="plan",
    )
    reviewed = _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert reviewed.status is PlanStatus.AWAITING_REVIEW
    assert reviewed.revision == 1
    assert reviewed.pending_clarification is None
    assert len(reviewed.clarification_history) == 1
    exchange = reviewed.clarification_history[0]
    assert tuple(answer.question_id for answer in exchange.answers) == (
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    )
    assert tuple(answer.answer for answer in exchange.answers) == (
        "Option 0",
        "Custom 1",
        "Option 2",
        "Custom 3",
    )
    review_interrupts = _root_interrupts(second)
    assert len(review_interrupts) == 1
    assert review_interrupts[0].value["kind"] == "tinkerfin:plan_review"


@pytest.mark.asyncio
async def test_agui_preserves_every_question_in_a_large_clarification_form() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner_clarification_batch()]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    events = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Deploy", id="agui-four-message")]},
        run_id="agui-four",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )

    assert not any(isinstance(event, RunErrorEvent) for event in events)
    terminal = _terminal(events)
    assert terminal.outcome is not None and terminal.outcome.type == "interrupt"
    assert len(terminal.outcome.interrupts) == 1
    interrupt = terminal.outcome.interrupts[0]
    assert interrupt.reason == "tinkerfin:plan_clarification"
    metadata = cast(dict[str, Any], interrupt.metadata)
    runtime_interrupt = cast(dict[str, Any], metadata["runtimeInterrupt"])
    envelope = cast(dict[str, Any], runtime_interrupt["envelope"])
    public_metadata = cast(dict[str, Any], envelope["metadata"])
    clarification = cast(dict[str, Any], public_metadata["clarification"])
    assert clarification["schema"] == "tinkerfin.plan-clarification.v2"
    form = cast(dict[str, Any], clarification["form"])
    assert form["schemaVersion"] == 2
    questions = cast(list[dict[str, Any]], form["questions"])
    assert [question["id"] for question in questions] == [
        "question-0",
        "question-1",
        "question-2",
        "question-3",
    ]
    assert [question["required"] for question in questions] == [True] * 4
    response_schema = cast(dict[str, Any], interrupt.response_schema)
    assert "ClarificationSkippedAnswer" in response_schema["$defs"]

    types = [event.type.value for event in events]
    state_index = len(types) - 1 - types[::-1].index("STATE_SNAPSHOT")
    messages_index = len(types) - 1 - types[::-1].index("MESSAGES_SNAPSHOT")
    terminal_index = len(types) - 1 - types[::-1].index("RUN_FINISHED")
    assert state_index < messages_index < terminal_index


@pytest.mark.asyncio
async def test_all_optional_clarification_questions_can_be_explicitly_skipped() -> None:
    model = _FakeModel(
        responses=[
            _planner_clarification_batch(2, required=False),
            _planner(goal="Proceed with explicit assumptions"),
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
        {"messages": [HumanMessage(content="Plan", id="optional-message")]},
        run_id="optional-1",
        config=config,
        mode="plan",
    )
    interrupt = _root_interrupts(first)[0]
    metadata = interrupt.value["metadata"]
    assert metadata["clarification"]["schema"] == "tinkerfin.plan-clarification.v2"
    form = metadata["clarification"]["form"]
    assert form["schemaVersion"] == 2
    assert [question["required"] for question in form["questions"]] == [False, False]

    second = await _parts(
        definition,
        Command(
            resume={
                "type": "respond",
                "answers": [
                    {"questionId": "question-0", "skipped": True},
                    {"questionId": "question-1", "skipped": True},
                ],
            }
        ),
        run_id="optional-2",
        config=config,
        mode="plan",
    )

    reviewed = _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
    assert reviewed.status is PlanStatus.AWAITING_REVIEW
    answers = reviewed.clarification_history[0].answers
    assert tuple(answer.question_id for answer in answers) == (
        "question-0",
        "question-1",
    )
    assert all(answer.skipped for answer in answers)
    assert all(answer.answer is None and answer.option_id is None for answer in answers)
    planner_context = "\n".join(
        str(message.content)
        for message in model.model_inputs[1]
        if isinstance(message, HumanMessage)
    )
    assert '"skipped": true' in planner_context


@pytest.mark.asyncio
async def test_required_clarification_question_cannot_be_skipped() -> None:
    definition = (
        TinkerFin()
        .plan(enabled=True)
        .create_deep_agent(
            model=_FakeModel(responses=[_planner_clarification(required=True)]),
            tools=[],
            checkpointer=InMemorySaver(),
        )
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    await _parts(
        definition,
        {"messages": [HumanMessage(content="Plan", id="required-message")]},
        run_id="required-1",
        config=config,
        mode="plan",
    )

    with pytest.raises(ValueError, match="cannot be skipped"):
        await _parts(
            definition,
            Command(
                resume={
                    "type": "respond",
                    "answers": [{"questionId": "target", "skipped": True}],
                }
            ),
            run_id="required-2",
            config=config,
            mode="plan",
        )


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
        _structured_plan_state(_root_values(second)[-1]["tinkerfin_plan"])
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
                "content": edited,
            }
        ),
        run_id="edit-2",
        config=config,
        mode="plan",
    )
    pending = _structured_plan_state(_root_values(clarification)[-1]["tinkerfin_plan"])
    assert pending.status is PlanStatus.AWAITING_CLARIFICATION
    assert pending.revision == 1
    assert pending.pending_edit is not None
    assert pending.pending_edit.goal == edited["goal"]
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
    final = _structured_plan_state(_root_values(reviewed)[-1]["tinkerfin_plan"])
    assert final.revision == 2
    assert final.draft is not None and final.draft.content.goal == edited["goal"]
    assert final.draft.content.steps[0].id == "edited-step"
    assert final.pending_edit is None


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
    plan = _structured_plan_state(_root_values(revised)[-1]["tinkerfin_plan"])
    assert plan.revision == 2
    assert plan.feedback == ("Add regression coverage",)
    assert plan.draft is not None and plan.draft.content.goal.endswith("revised")


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
    plan = _structured_plan_state(_root_values(rejected)[-1]["tinkerfin_plan"])
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
        payload={"type": "approve", "baseRevision": 1},
    )
    interrupted = await _agui_events(
        definition,
        None,
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
    resume_binding = AgUiResumeBinding.from_agui(
        entries=(_resume_entry(tool_interrupt.id, {"type": "approve"}),),
        interrupts=tool_outcome.interrupts,
    )
    completed = await _agui_events(
        definition,
        None,
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
    resumed_plan = _structured_plan_state(
        completed_snapshots[-1].snapshot["tinkerfin_plan"]
    )
    assert resumed_plan.status is PlanStatus.APPROVED
    assert resumed_plan.effective_mode == "default"
    assert resumed_plan.handoff is not None
    assert resumed_plan.handoff.phase is PlanHandoffPhase.ACCEPTED
    assert any(
        operation.get("path", "").endswith("/handoff/phase")
        and operation.get("value") == PlanHandoffPhase.COMPLETED.value
        for event in completed
        if isinstance(event, StateDeltaEvent)
        for operation in event.delta
    )


@pytest.mark.asyncio
async def test_plan_enabled_default_subagent_resume_uses_only_the_native_head(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Run the reviewed child tool",
                            "subagent_type": "general-purpose",
                        },
                        "id": "parent-task-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "approved_tool",
                        "args": {"value": "child"},
                        "id": "child-approved-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="child done"),
            AIMessage(content="root done"),
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
    thread_id = "plan-enabled-subagent-resume"
    config = {"configurable": {"thread_id": thread_id}}
    review = await _agui_events(
        definition,
        {"messages": [HumanMessage(content="Delegate", id="message")]},
        run_id="subagent-review",
        config=config,
        thread_id=thread_id,
        mode="default",
    )
    outcome = _terminal(review).outcome
    assert outcome is not None and outcome.type == "interrupt"
    interrupt = outcome.interrupts[0]
    assert interrupt.tool_call_id is not None
    assert ScopedIdCodec().decode(interrupt.tool_call_id)[1]
    binding = AgUiResumeBinding.from_agui(
        entries=(_resume_entry(interrupt.id, {"type": "approve"}),),
        interrupts=outcome.interrupts,
    )

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="langgraph")
    completed = await _agui_events(
        definition,
        None,
        run_id="subagent-resume",
        config=config,
        thread_id=thread_id,
        mode="default",
        resume=binding,
    )

    assert _terminal(completed).outcome.type == "success"
    assert not any(
        "Ignoring unknown node name" in record.getMessage() for record in caplog.records
    )


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
