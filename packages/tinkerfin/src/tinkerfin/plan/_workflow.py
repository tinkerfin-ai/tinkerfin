"""Independent parent LangGraph that wraps one existing Deep Agent execution graph."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, cast

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

from ._clarification import restore_form, serialize_form
from ._config import (
    PLAN_MODE_CONFIG_KEY,
    PlanOptions,
    validate_agent_mode,
)
from ._contracts import (
    CLARIFICATION_RESPONSE,
    PLAN_REVIEW_RESPONSE,
    ApprovePlan,
    ClarificationFreeTextAnswer,
    ClarificationOptionAnswer,
    EditPlan,
    PlanClarificationMetadata,
    PlanClarificationPayload,
    RejectPlan,
    RespondToPlan,
)
from ._gate import create_gate_agent, invoke_gate
from ._middleware import ConfirmedPlanMiddleware
from ._planner import create_planner_agent, invoke_planner
from ._state import (
    PLAN_SCHEMA_FINGERPRINT_KEY,
    PlanWorkflowNodeState,
    create_plan_state_schema,
    plan_state_update,
    read_plan_state,
)
from .errors import PlanModeConfigurationError
from .models import (
    ClarificationExchange,
    ConfirmedPlan,
    PendingClarification,
    PlanDraft,
    PlanReviewAction,
    PlanRoute,
    PlanState,
    PlanStatus,
    RequirementAnswer,
)

_NativeFactory = Callable[..., object]
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
_SchemaT = TypeVar("_SchemaT")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class _CompiledPlanRuntime(Protocol):
    """Typed subset of the locked CompiledStateGraph used by Plan."""

    checkpointer: object
    store: BaseStore | None

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...


_SyncPlanNode: TypeAlias = Callable[
    [PlanWorkflowNodeState],
    dict[str, object],
]
_ConfigPlanNode: TypeAlias = Callable[
    [PlanWorkflowNodeState, RunnableConfig],
    dict[str, object],
]
_AsyncConfigPlanNode: TypeAlias = Callable[
    [PlanWorkflowNodeState, RunnableConfig],
    Awaitable[dict[str, object]],
]
_PlanNode: TypeAlias = (
    _SyncPlanNode | _ConfigPlanNode | _AsyncConfigPlanNode | _CompiledPlanRuntime
)
_PlanPath: TypeAlias = (
    Callable[[PlanWorkflowNodeState], str]
    | Callable[[PlanWorkflowNodeState, RunnableConfig], str]
)


class _PlanGraphBuilder(Protocol):
    """Typed subset of StateGraph isolated from third-party unknown generics."""

    def add_node(self, node: str, action: _PlanNode) -> object: ...

    def add_edge(self, start_key: str, end_key: str) -> object: ...

    def add_conditional_edges(
        self,
        source: str,
        path: _PlanPath,
        path_map: Mapping[str, str],
    ) -> object: ...

    def compile(
        self,
        *,
        checkpointer: _CheckpointSaver,
        store: BaseStore | None,
        cache: BaseCache[object] | None,
        name: str,
    ) -> _CompiledPlanRuntime: ...


class _SignatureCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


_COMPILED_ASTREAM = cast(
    _SignatureCallable,
    CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
)


def _messages(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    value = state.get("messages")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("Plan workflow state requires a message sequence")
    sequence = cast(Sequence[object], value)
    messages = tuple(item for item in sequence if isinstance(item, BaseMessage))
    if len(messages) != len(sequence):
        raise TypeError("Plan workflow messages must be LangChain message objects")
    return messages


def _clarification_context(
    plan: PlanState,
    options: PlanOptions,
) -> tuple[dict[str, JsonValue], ...]:
    context: list[dict[str, JsonValue]] = []
    for exchange in plan.clarification_history:
        form = restore_form(
            options.clarification,
            exchange.form,
        )
        context.append(
            _JSON_OBJECT.validate_python(
                {
                    "source": exchange.source,
                    "form": form.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    ),
                    "answers": [
                        answer.model_dump(mode="json", by_alias=True)
                        for answer in exchange.answers
                    ],
                }
            )
        )
    return tuple(context)


def _clarification_summary(
    context: Sequence[Mapping[str, object]],
) -> str | None:
    if not context:
        return None
    return json.dumps(list(context), ensure_ascii=False, indent=2)


def _require_schema_fingerprint(
    state: Mapping[str, object],
    options: PlanOptions,
) -> None:
    if state.get(PLAN_SCHEMA_FINGERPRINT_KEY) != options.clarification.fingerprint:
        raise PlanModeConfigurationError(
            "checkpoint clarification schema does not match this Definition"
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


def _checkpoint_saver(value: object) -> _CheckpointSaver:
    if not isinstance(value, BaseCheckpointSaver):
        raise PlanModeConfigurationError(
            "Plan Mode requires a concrete BaseCheckpointSaver"
        )
    return cast(_CheckpointSaver, value)


def _optional_cache(value: object) -> BaseCache[object] | None:
    if value is None:
        return None
    if not isinstance(value, BaseCache):
        raise PlanModeConfigurationError("Plan Mode cache must be a BaseCache or None")
    return cast(BaseCache[object], value)


class PlanWorkflowGraph(Generic[ContextT]):
    """Compiled parent workflow with a mandatory synchronous durability boundary."""

    __slots__ = ("_graph",)

    def __init__(
        self,
        graph: _CompiledPlanRuntime,
    ) -> None:
        self._graph = graph

    @property
    def checkpointer(self) -> _CheckpointSaver:
        """Return the concrete checkpointer owned by the parent graph."""

        return _checkpoint_saver(self._graph.checkpointer)

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
        return self._graph.astream(*args, **options)


setattr(
    PlanWorkflowGraph.astream,
    "__signature__",
    inspect.signature(_COMPILED_ASTREAM),
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
        _checkpoint_saver(arguments.get("checkpointer"))

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
        deep_agent_runtime = cast(_CompiledPlanRuntime, deep_agent)

        gate = create_gate_agent(
            self._options.gate_model or model,
            clarification=self._options.clarification,
            context_schema=context_schema,
        )
        planner = create_planner_agent(
            self._options.planner_model or model,
            backend=backend,
            clarification=self._options.clarification,
            context_schema=context_schema,
        )

        async def gate_node(
            state: PlanWorkflowNodeState,
            config: RunnableConfig,
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprint(mapped, self._options)
            current = read_plan_state(mapped)
            clarification_context = _clarification_context(current, self._options)
            decision = await invoke_gate(
                gate,
                _messages(mapped),
                clarification=self._options.clarification,
                config=config,
                requirement_summary=_clarification_summary(clarification_context),
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
            pending: PendingClarification | None = None
            if decision.route is PlanRoute.CLARIFY:
                _, form_payload = serialize_form(
                    self._options.clarification,
                    decision.clarification,
                )
                pending = PendingClarification(
                    source="gate",
                    form=form_payload,
                )
            updated = current.model_copy(
                update={
                    "status": status,
                    "route": decision.route,
                    "goal": decision.goal,
                    "pending_clarification": pending,
                    "review_action": None,
                }
            )
            return plan_state_update(updated)

        async def planner_node(
            state: PlanWorkflowNodeState,
            config: RunnableConfig,
        ) -> dict[str, object]:
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprint(mapped, self._options)
            current = read_plan_state(mapped)
            clarification_context = _clarification_context(current, self._options)
            outcome = await invoke_planner(
                planner,
                _messages(mapped),
                current,
                clarification=self._options.clarification,
                clarification_history=clarification_context,
                config=config,
                files=mapped.get("files"),
            )
            if outcome.type == "clarify":
                _, form_payload = serialize_form(
                    self._options.clarification,
                    outcome.clarification,
                )
                updated = current.model_copy(
                    update={
                        "status": PlanStatus.AWAITING_CLARIFICATION,
                        "route": PlanRoute.PLAN,
                        "pending_clarification": PendingClarification(
                            source="planner",
                            form=form_payload,
                        ),
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
                        "pending_clarification": None,
                        "draft": draft,
                        "revision": revision,
                        "review_action": None,
                    }
                )
            return plan_state_update(updated)

        def initialize_node(state: PlanWorkflowNodeState) -> dict[str, object]:
            del state
            return {
                **plan_state_update(PlanState()),
                PLAN_SCHEMA_FINGERPRINT_KEY: self._options.clarification.fingerprint,
            }

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
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprint(mapped, self._options)
            current = read_plan_state(mapped)
            pending = current.pending_clarification
            if pending is None:
                raise RuntimeError("Plan clarification requires a pending form")
            if pending.source != source:
                raise RuntimeError("Plan clarification resumed at the wrong source")
            form = restore_form(
                self._options.clarification,
                pending.form,
            )
            metadata = PlanClarificationMetadata(
                source=source,
                clarification=PlanClarificationPayload(form=pending.form),
            )
            envelope = RuntimeInterruptEnvelope(
                kind="plan_clarification",
                message="Answer the blocking questions before planning continues.",
                response_schema=_json_schema(CLARIFICATION_RESPONSE),
                metadata=_JSON_OBJECT.validate_python(
                    metadata.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                ),
            )
            raw_response = interrupt(_runtime_interrupt_value(envelope))
            response = CLARIFICATION_RESPONSE.validate_python(raw_response)
            answers = {answer.question_id: answer for answer in response.answers}
            expected = tuple(question.id for question in form.questions)
            if set(answers) != set(expected) or len(answers) != len(expected):
                raise ValueError(
                    "clarification response must answer every pending question exactly once"
                )
            ordered_answers: list[RequirementAnswer] = []
            for question in form.questions:
                answer = answers[question.id]
                if isinstance(answer, ClarificationOptionAnswer):
                    selected = next(
                        (
                            option
                            for option in question.options
                            if option.id == answer.option_id
                        ),
                        None,
                    )
                    if selected is None:
                        raise ValueError(
                            f"clarification answer selected an unknown option for {question.id!r}"
                        )
                    normalized = RequirementAnswer(
                        question_id=question.id,
                        option_id=selected.id,
                        answer=selected.label,
                    )
                elif isinstance(answer, ClarificationFreeTextAnswer):
                    if not question.allow_free_text:
                        raise ValueError(
                            f"clarification question {question.id!r} requires an option"
                        )
                    normalized = RequirementAnswer(
                        question_id=question.id,
                        answer=answer.answer,
                    )
                else:  # pragma: no cover - Pydantic union is exhaustive
                    raise TypeError("unsupported clarification answer")
                ordered_answers.append(normalized)
            ordered = tuple(ordered_answers)
            exchange = ClarificationExchange(
                source=pending.source,
                form=pending.form,
                answers=ordered,
            )
            updated = current.model_copy(
                update={
                    "status": PlanStatus.PLANNING,
                    "route": (None if source == "gate" else PlanRoute.PLAN),
                    "pending_clarification": None,
                    "clarification_history": (
                        *current.clarification_history,
                        exchange,
                    ),
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
            mapped = cast(Mapping[str, object], state)
            _require_schema_fingerprint(mapped, self._options)
            current = read_plan_state(mapped)
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
            return "clarify" if current.pending_clarification is not None else "review"

        def review_path(state: PlanWorkflowNodeState) -> str:
            action = read_plan_state(cast(Mapping[str, object], state)).review_action
            if action is None:
                raise RuntimeError("Plan review did not record an action")
            return action.value

        builder = cast(
            _PlanGraphBuilder,
            StateGraph(state_schema, context_schema=context_schema),
        )
        builder.add_node("initialize_plan", initialize_node)
        builder.add_node("plan_gate", gate_node)
        builder.add_node("clarify_gate", clarify_gate_node)
        builder.add_node("create_plan", planner_node)
        builder.add_node("clarify_planner", clarify_planner_node)
        builder.add_node("review_plan", review_node)
        builder.add_node("execute_deep_agent", deep_agent_runtime)
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

        checkpointer = _checkpoint_saver(arguments["checkpointer"])
        store = cast(BaseStore | None, arguments.get("store"))
        cache = _optional_cache(arguments.get("cache"))
        parent = builder.compile(
            checkpointer=checkpointer,
            store=store,
            cache=cache,
            name="tinkerfin_plan_workflow",
        )
        return PlanWorkflowGraph[ContextT](parent)


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
