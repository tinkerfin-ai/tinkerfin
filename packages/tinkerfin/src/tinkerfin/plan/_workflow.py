"""Independent parent LangGraph that wraps one existing Deep Agent execution graph."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Generic, Literal, TypeVar, cast

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.cache.base import BaseCache
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import interrupt
from langgraph.typing import ContextT
from pydantic import JsonValue, TypeAdapter

from tinkerfin_agui_adapter import RuntimeInterruptEnvelope

from ._config import (
    PLAN_MODE_CONFIG_KEY,
    PlanOptions,
    validate_agent_mode,
)
from ._contracts import (
    CLARIFICATION_RESPONSE,
    PLAN_REVIEW_RESPONSE,
    ApprovePlan,
    EditPlan,
    RejectPlan,
    RespondToPlan,
)
from ._gate import create_gate_agent, invoke_gate
from ._middleware import ConfirmedPlanMiddleware
from ._planner import create_planner_agent, invoke_planner
from ._state import (
    PlanWorkflowNodeState,
    create_plan_state_schema,
    plan_state_update,
    read_plan_state,
)
from .errors import PlanModeConfigurationError
from .models import (
    ConfirmedPlan,
    PlanDraft,
    PlanReviewAction,
    PlanRoute,
    PlanState,
    PlanStatus,
)

_NativeFactory = Callable[..., object]
_GraphAstream = Callable[..., AsyncIterator[Mapping[str, object]]]
_SchemaT = TypeVar("_SchemaT")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _messages(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    value = state.get("messages")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("Plan workflow state requires a message sequence")
    messages = tuple(item for item in value if isinstance(item, BaseMessage))
    if len(messages) != len(value):
        raise TypeError("Plan workflow messages must be LangChain message objects")
    return messages


def _requirement_summary(plan: PlanState) -> str | None:
    if not plan.requirements:
        return None
    return "\n".join(
        f"- {answer.question_id}: {answer.answer}" for answer in plan.requirements
    )


def _json_schema(adapter: TypeAdapter[_SchemaT]) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_python(adapter.json_schema(by_alias=True))


def _runtime_interrupt_value(
    envelope: RuntimeInterruptEnvelope,
) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_python(
        envelope.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    )


class PlanWorkflowGraph(Generic[ContextT]):
    """Compiled parent workflow with a mandatory synchronous durability boundary."""

    __slots__ = ("_graph",)

    def __init__(
        self,
        graph: CompiledStateGraph[
            DeepAgentState,
            ContextT,
            DeepAgentState,
            DeepAgentState,
        ],
    ) -> None:
        self._graph = graph

    @property
    def checkpointer(self) -> BaseCheckpointSaver:
        """Return the concrete checkpointer owned by the parent graph."""

        return cast(BaseCheckpointSaver, self._graph.checkpointer)

    @property
    def store(self) -> BaseStore | None:
        """Return the optional Store owned by the parent graph."""

        return self._graph.store

    def astream(
        self, *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        """Stream the parent graph with synchronously committed Plan boundaries."""

        options = dict(kwargs)
        durability = options.get("durability")
        if durability not in (None, "sync"):
            raise PlanModeConfigurationError("Plan Mode requires durability='sync'")
        options["durability"] = "sync"
        stream = cast(_GraphAstream, self._graph.astream)
        return stream(*args, **options)


setattr(
    PlanWorkflowGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


class _PlanGraphFactory(Generic[ContextT]):
    """Deferred builder that keeps borrowed upstream arguments and fixed options."""

    __slots__ = ("_native_factory", "_options", "_signature")

    def __init__(
        self,
        native_factory: _NativeFactory,
        signature: inspect.Signature,
        initial: inspect.BoundArguments,
        options: PlanOptions,
    ) -> None:
        self._native_factory = native_factory
        self._signature = signature
        self._options = options
        initial.apply_defaults()
        self._validate_configuration(initial.arguments)

    def __call__(self, *args: object, **kwargs: object) -> PlanWorkflowGraph[ContextT]:
        bound = self._signature.bind(*args, **kwargs)
        bound.apply_defaults()
        self._validate_configuration(bound.arguments)
        return self._build(args, kwargs, bound.arguments)

    @staticmethod
    def _validate_configuration(arguments: Mapping[str, object]) -> None:
        model = arguments.get("model")
        if not isinstance(model, (str, BaseChatModel)) or (
            isinstance(model, str) and not model.strip()
        ):
            raise PlanModeConfigurationError("Plan Mode requires an explicit model")
        checkpointer = arguments.get("checkpointer")
        if not isinstance(checkpointer, BaseCheckpointSaver):
            raise PlanModeConfigurationError(
                "Plan Mode requires a concrete BaseCheckpointSaver"
            )

    def _build(
        self,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        arguments: Mapping[str, object],
    ) -> PlanWorkflowGraph[ContextT]:
        model = cast(str | BaseChatModel, arguments["model"])
        context_schema = cast(
            type[ContextT] | None,
            arguments.get("context_schema"),
        )
        backend_value = arguments.get("backend")
        backend = (
            StateBackend()
            if backend_value is None
            else cast(BackendProtocol, backend_value)
        )
        base_state_schema = cast(
            type[DeepAgentState] | None,
            arguments.get("state_schema"),
        )
        caller_middleware = cast(
            Sequence[AgentMiddleware],  # pyright: ignore[reportMissingTypeArgument]
            arguments.get("middleware", ()),
        )
        state_schema = create_plan_state_schema(
            base_state_schema,
            middleware=caller_middleware,
        )

        child_kwargs = dict(kwargs)
        child_kwargs.update(
            {
                "backend": backend,
                "middleware": (*caller_middleware, ConfirmedPlanMiddleware()),
                "state_schema": state_schema,
                "checkpointer": None,
                "store": None,
                "cache": None,
            }
        )
        deep_agent = self._native_factory(*args, **child_kwargs)
        if not isinstance(deep_agent, CompiledStateGraph):
            raise TypeError("Deep Agent factory did not return a CompiledStateGraph")

        gate = create_gate_agent(
            self._options.gate_model or model,
            context_schema=context_schema,
        )
        planner = create_planner_agent(
            self._options.planner_model or model,
            backend=backend,
            context_schema=context_schema,
        )

        async def gate_node(state: PlanWorkflowNodeState) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            current = read_plan_state(mapped)
            decision = await invoke_gate(
                gate,
                _messages(mapped),
                requirement_summary=_requirement_summary(current),
            )
            status = (
                PlanStatus.EXECUTING
                if decision.route is PlanRoute.DIRECT
                else (
                    PlanStatus.AWAITING_CLARIFICATION
                    if decision.route is PlanRoute.CLARIFY
                    else PlanStatus.PLANNING
                )
            )
            updated = current.model_copy(
                update={
                    "status": status,
                    "route": decision.route,
                    "goal": decision.goal,
                    "questions": decision.questions,
                    "review_action": None,
                }
            )
            return plan_state_update(updated)

        async def planner_node(state: PlanWorkflowNodeState) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            current = read_plan_state(mapped)
            outcome = await invoke_planner(
                planner,
                _messages(mapped),
                current,
                files=mapped.get("files"),
            )
            if outcome.type == "clarify":
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_CLARIFICATION,
                        "route": PlanRoute.PLAN,
                        "questions": outcome.questions,
                        "review_action": None,
                    }
                )
            else:
                assert outcome.draft is not None
                revision = current.revision + 1
                draft = PlanDraft(
                    revision=revision,
                    **outcome.draft.model_dump(mode="python", by_alias=False),
                )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_REVIEW,
                        "route": PlanRoute.PLAN,
                        "goal": draft.goal,
                        "questions": (),
                        "draft": draft,
                        "revision": revision,
                        "review_action": None,
                    }
                )
            return plan_state_update(updated)

        def initialize_node(state: PlanWorkflowNodeState) -> dict[str, object]:
            del state
            return plan_state_update(PlanState())

        def mode_path(
            state: PlanWorkflowNodeState,
            config: RunnableConfig,
        ) -> str:
            del state
            configurable = config.get("configurable", {})
            raw_mode = configurable.get(
                PLAN_MODE_CONFIG_KEY,
                self._options.default_mode,
            )
            return validate_agent_mode(raw_mode)

        def answer_clarification(
            state: PlanWorkflowNodeState,
            *,
            source: Literal["gate", "planner"],
        ) -> dict[str, object]:
            current = read_plan_state(cast(Mapping[str, object], state))
            envelope = RuntimeInterruptEnvelope(
                kind="plan_clarification",
                message="Answer the blocking questions before planning continues.",
                response_schema=_json_schema(CLARIFICATION_RESPONSE),
                metadata={
                    "origin": "plan",
                    "source": source,
                    "questionIds": [question.id for question in current.questions],
                    "questions": [
                        question.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=False,
                        )
                        for question in current.questions
                    ],
                },
            )
            raw_response = interrupt(_runtime_interrupt_value(envelope))
            response = CLARIFICATION_RESPONSE.validate_python(raw_response)
            answers = {answer.question_id: answer for answer in response.answers}
            expected = tuple(question.id for question in current.questions)
            if set(answers) != set(expected) or len(answers) != len(expected):
                raise ValueError(
                    "clarification response must answer every pending question exactly once"
                )
            ordered_answers = []
            for question in current.questions:
                answer = answers[question.id]
                option_ids = {option.id for option in question.options}
                if answer.option_id is not None:
                    if answer.option_id not in option_ids:
                        raise ValueError(
                            f"clarification answer selected an unknown option for {question.id!r}"
                        )
                elif not question.allow_custom_answer:
                    raise ValueError(
                        f"clarification question {question.id!r} requires an option"
                    )
                ordered_answers.append(answer)
            ordered = tuple(ordered_answers)
            updated = current.model_copy(
                update={
                    "status": PlanStatus.PLANNING,
                    "route": (None if source == "gate" else PlanRoute.PLAN),
                    "questions": (),
                    "requirements": (*current.requirements, *ordered),
                }
            )
            return plan_state_update(updated)

        def clarify_gate_node(
            state: PlanWorkflowNodeState,
        ) -> dict[str, object]:
            return answer_clarification(state, source="gate")

        def clarify_planner_node(
            state: PlanWorkflowNodeState,
        ) -> dict[str, object]:
            return answer_clarification(state, source="planner")

        def review_node(state: PlanWorkflowNodeState) -> dict[str, object]:
            current = read_plan_state(cast(Mapping[str, object], state))
            draft = current.draft
            if draft is None:
                raise RuntimeError("Plan review requires a current draft")
            envelope = RuntimeInterruptEnvelope(
                kind="plan_review",
                message="Review the proposed Plan before execution begins.",
                response_schema=_json_schema(PLAN_REVIEW_RESPONSE),
                metadata={
                    "origin": "plan",
                    "planRevision": draft.revision,
                    "draft": draft.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    ),
                },
            )
            raw_response = interrupt(_runtime_interrupt_value(envelope))
            response = PLAN_REVIEW_RESPONSE.validate_python(raw_response)
            if response.base_revision != draft.revision:
                raise ValueError("Plan review baseRevision is stale")

            if isinstance(response, ApprovePlan):
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.EXECUTING,
                        "confirmed_plan": ConfirmedPlan.from_draft(draft),
                        "review_action": PlanReviewAction.APPROVE,
                    }
                )
            elif isinstance(response, EditPlan):
                revision = current.revision + 1
                edited = PlanDraft(
                    revision=revision,
                    **response.draft.model_dump(mode="python", by_alias=False),
                )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_REVIEW,
                        "goal": edited.goal,
                        "draft": edited,
                        "revision": revision,
                        "review_action": PlanReviewAction.EDIT,
                    }
                )
            elif isinstance(response, RespondToPlan):
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.PLANNING,
                        "feedback": (*current.feedback, response.message),
                        "review_action": PlanReviewAction.RESPOND,
                    }
                )
            elif isinstance(response, RejectPlan):
                feedback = (
                    current.feedback
                    if response.message is None
                    else (*current.feedback, response.message)
                )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.CANCELLED,
                        "feedback": feedback,
                        "review_action": PlanReviewAction.REJECT,
                    }
                )
            else:  # pragma: no cover - discriminated adapter is exhaustive
                raise TypeError("unsupported Plan review response")
            return plan_state_update(updated)

        def complete_node(state: PlanWorkflowNodeState) -> dict[str, object]:
            current = read_plan_state(cast(Mapping[str, object], state))
            return plan_state_update(
                current.model_copy(update={"status": PlanStatus.COMPLETED})
            )

        def gate_path(state: PlanWorkflowNodeState) -> str:
            route = read_plan_state(cast(Mapping[str, object], state)).route
            if route is None:
                raise RuntimeError("Gate did not select a route")
            return route.value

        def planner_path(state: PlanWorkflowNodeState) -> str:
            current = read_plan_state(cast(Mapping[str, object], state))
            return "clarify" if current.questions else "review"

        def review_path(state: PlanWorkflowNodeState) -> str:
            action = read_plan_state(cast(Mapping[str, object], state)).review_action
            if action is None:
                raise RuntimeError("Plan review did not record an action")
            return action.value

        builder = StateGraph(state_schema, context_schema=context_schema)
        builder.add_node("initialize_plan", initialize_node)
        builder.add_node("plan_gate", gate_node)
        builder.add_node("clarify_gate", clarify_gate_node)
        builder.add_node("create_plan", planner_node)
        builder.add_node("clarify_planner", clarify_planner_node)
        builder.add_node("review_plan", review_node)
        builder.add_node("execute_deep_agent", deep_agent)
        builder.add_node("complete_plan", complete_node)
        builder.add_edge(START, "initialize_plan")
        builder.add_conditional_edges(
            "initialize_plan",
            mode_path,
            {
                "default": "execute_deep_agent",
                "plan": "plan_gate",
            },
        )
        builder.add_conditional_edges(
            "plan_gate",
            gate_path,
            {
                "direct": "execute_deep_agent",
                "clarify": "clarify_gate",
                "plan": "create_plan",
            },
        )
        builder.add_edge("clarify_gate", "plan_gate")
        builder.add_conditional_edges(
            "create_plan",
            planner_path,
            {"clarify": "clarify_planner", "review": "review_plan"},
        )
        builder.add_edge("clarify_planner", "create_plan")
        builder.add_conditional_edges(
            "review_plan",
            review_path,
            {
                "approve": "execute_deep_agent",
                "edit": "review_plan",
                "respond": "create_plan",
                "reject": END,
            },
        )
        builder.add_edge("execute_deep_agent", "complete_plan")
        builder.add_edge("complete_plan", END)

        checkpointer = cast(BaseCheckpointSaver, arguments["checkpointer"])
        store = cast(BaseStore | None, arguments.get("store"))
        cache = cast(BaseCache | None, arguments.get("cache"))  # pyright: ignore[reportMissingTypeArgument]
        parent = builder.compile(
            checkpointer=checkpointer,
            store=store,
            cache=cache,
            name="tinkerfin_plan_workflow",
        )
        return PlanWorkflowGraph(parent)


def prepare_plan_factory(
    native_factory: _NativeFactory,
    signature: inspect.Signature,
    initial: inspect.BoundArguments,
    options: PlanOptions,
) -> _NativeFactory:
    """Validate Plan options and return one deferred parent-workflow factory."""

    return cast(
        _NativeFactory,
        _PlanGraphFactory(native_factory, signature, initial, options),
    )


__all__ = ["PlanWorkflowGraph", "prepare_plan_factory"]
