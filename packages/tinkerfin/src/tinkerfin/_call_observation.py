"""LangChain call observation and explicit semantic contribution lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langgraph.errors import GraphBubbleUp, GraphInterrupt
from pydantic import JsonValue

from tinkerfin_contracts import (
    AgentStepObservation,
    ContextContributionObservation,
    ContextKind,
    ModelCallObservation,
    ObservationBoundary,
    RunTerminalOutcome,
    ToolExecutionObservation,
)

from ._observation import (
    _json_object,
    _message_record,
    _qualified_name,
    _source_value,
    _stamp,
)
from .errors import TinkerFinStreamProtocolError

if TYPE_CHECKING:
    from ._observation import RuntimeObservationHub

_CallTerminalPhase = Literal[
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "abandoned",
]
_ModelTerminalPhase = _CallTerminalPhase
_ToolTerminalPhase = _CallTerminalPhase


_CURRENT_HUB: ContextVar[RuntimeObservationHub | None] = ContextVar(
    "tinkerfin_current_observation_hub",
    default=None,
)
_CURRENT_CALL_ID: ContextVar[str | None] = ContextVar(
    "tinkerfin_current_call_id",
    default=None,
)
_CURRENT_NAMESPACE: ContextVar[tuple[str, ...]] = ContextVar(
    "tinkerfin_current_call_namespace",
    default=(),
)


def bind_observation_hub(
    hub: RuntimeObservationHub,
) -> Token[RuntimeObservationHub | None]:
    """Bind one Runtime hub to work executed by the current upstream pull."""

    return _CURRENT_HUB.set(hub)


def reset_observation_hub(token: Token[RuntimeObservationHub | None]) -> None:
    """Restore the observation context after one upstream pull settles."""

    _CURRENT_HUB.reset(token)


_MIDDLEWARE_NODE_HOOKS = frozenset(
    {"before_agent", "before_model", "after_model", "after_agent"}
)


def _checkpoint_segments(
    metadata: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Return canonical locked checkpoint segments without assigning semantics."""

    if metadata is None:
        return ()
    raw_namespace = metadata.get("langgraph_checkpoint_ns")
    if not isinstance(raw_namespace, str) or not raw_namespace:
        return ()
    segments = tuple(raw_namespace.split("|"))
    return () if any(not segment for segment in segments) else segments


def _callback_namespace(metadata: Mapping[str, object] | None) -> tuple[str, ...]:
    """Resolve the locked LangGraph callback scope to its Native namespace.

    LangGraph 1.2.10 appends the current graph node to
    ``langgraph_checkpoint_ns``. Removing that final segment yields the same child
    namespace carried by the validated v2 Native stream. The locked dependency contract
    test protects this mapping for root and subagent calls.
    """

    segments = _checkpoint_segments(metadata)
    return segments[:-1]


def _callback_task_id(metadata: Mapping[str, object] | None) -> str | None:
    """Read the Native task ID embedded in the locked callback checkpoint scope."""

    segments = _checkpoint_segments(metadata)
    if not segments:
        return None
    _node, separator, task_id = segments[-1].partition(":")
    return task_id if separator and task_id else None


def _optional_text(value: object) -> str | None:
    return (
        value if isinstance(value, str) and value and value == value.strip() else None
    )


def _agent_name(metadata: Mapping[str, object] | None) -> str | None:
    return None if metadata is None else _optional_text(metadata.get("lc_agent_name"))


def _provider_and_model(
    metadata: Mapping[str, object] | None,
    invocation: Mapping[str, object],
) -> tuple[str | None, str | None]:
    provider = None if metadata is None else _optional_text(metadata.get("ls_provider"))
    model = next(
        (
            text
            for key in ("model", "model_name", "model_id")
            if (text := _optional_text(invocation.get(key))) is not None
        ),
        None,
    )
    if model is None and metadata is not None:
        model = _optional_text(metadata.get("ls_model_name"))
    return provider, model


def _parent_id(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _error_message(error: BaseException) -> str | None:
    """Return one bounded exception message for observer-controlled retention."""

    message = str(error).strip()
    return message if message and len(message) <= 4096 else None


def _control_flow_phase(
    error: BaseException,
) -> Literal["cancelled", "interrupted", "abandoned"] | None:
    """Classify process control without turning it into an execution failure."""

    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, GraphInterrupt):
        return "interrupted"
    if isinstance(error, GraphBubbleUp):
        return "abandoned"
    return None


def _settles_with_run(error: BaseException) -> bool:
    """Return whether only the authoritative Run terminal can classify closure."""

    return isinstance(error, GeneratorExit)


def _response_values(
    response: LLMResult,
) -> tuple[dict[str, JsonValue] | None, dict[str, JsonValue] | None]:
    usage: dict[str, JsonValue] | None = None
    response_metadata: dict[str, JsonValue] | None = None
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, BaseMessage):
                continue
            raw_usage = getattr(message, "usage_metadata", None)
            if raw_usage is not None:
                usage = _json_object(raw_usage)
            if message.response_metadata:
                response_metadata = _json_object(message.response_metadata)
            break
        if usage is not None or response_metadata is not None:
            break
    if usage is None and isinstance(response.llm_output, Mapping):
        raw_usage = response.llm_output.get("token_usage")
        if raw_usage is None:
            raw_usage = response.llm_output.get("usage")
        if raw_usage is not None:
            normalized = _source_value(raw_usage)
            if isinstance(normalized, dict):
                usage = normalized
    return usage, response_metadata


def _response_tool_call_ids(response: LLMResult) -> tuple[str, ...]:
    """Return stable Tool proposal IDs from one completed provider response."""

    values: list[str] = []
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, BaseMessage):
                continue
            raw_calls: object = getattr(message, "tool_calls", ())
            if not isinstance(raw_calls, list | tuple):
                continue
            for call in cast(Sequence[object], raw_calls):
                if not isinstance(call, Mapping):
                    continue
                resolved_call = cast(Mapping[str, object], call)
                call_id = _optional_text(resolved_call.get("id"))
                if call_id is not None and call_id not in values:
                    values.append(call_id)
    return tuple(values)


def _response_message_ids(response: LLMResult) -> tuple[str, ...]:
    """Return stable message identities from one completed chat-model response."""

    values: list[str] = []
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, BaseMessage):
                continue
            message_id = _optional_text(message.id)
            if message_id is not None and message_id not in values:
                values.append(message_id)
    return tuple(values)


def _chunk_message_ids(value: object) -> tuple[str, ...]:
    """Return the message identity carried by a locked callback chunk, if present."""

    message = getattr(value, "message", None)
    if not isinstance(message, BaseMessage):
        return ()
    message_id = _optional_text(message.id)
    return () if message_id is None else (message_id,)


@dataclass(frozen=True, slots=True)
class _ModelCallState:
    parent_call_id: str | None
    namespace: tuple[str, ...]
    agent_name: str | None


@dataclass(frozen=True, slots=True)
class _ToolCallState:
    parent_call_id: str | None
    namespace: tuple[str, ...]
    agent_name: str | None
    tool_call_id: str | None
    tool_name: str


@dataclass(frozen=True, slots=True)
class _AgentStepState:
    parent_call_id: str | None
    namespace: tuple[str, ...]
    agent_name: str | None
    step_kind: Literal["agent", "middleware", "model", "tools", "subagent", "task"]
    name: str
    task_id: str | None
    middleware_name: str | None
    hook: str | None


class RuntimeCallHandler(AsyncCallbackHandler):
    """Translate one managed LangChain callback tree into Runtime observations.

    The handler is request-scoped and borrowed by LangChain through the invocation
    config. It records standard Agent steps plus provider and Tool callbacks. Native
    state, task payloads, interrupts, messages, and subagent completion remain owned by
    the Native Driver. Parallel callbacks are serialized before Observer delivery, while
    callback methods still await every accepted observation and preserve cancellation.
    """

    def __init__(self, hub: RuntimeObservationHub) -> None:
        self.raise_error = True
        # LangChain otherwise schedules this handler in a sibling task, so the Tool or
        # provider body cannot inherit the call scope established by its start callback.
        # Inline callbacks preserve that public contribution-to-call relationship while
        # the hub remains the single ordered delivery boundary.
        self.run_inline = True
        self._hub = hub
        self._steps: dict[str, _AgentStepState] = {}
        self._graph_tasks: dict[tuple[tuple[str, ...], str], str] = {}
        self._agents_by_namespace: dict[tuple[str, ...], str] = {}
        self._models: dict[str, _ModelCallState] = {}
        self._tools: dict[str, _ToolCallState] = {}
        self._first_outputs: set[str] = set()
        self._call_tokens: dict[str, Token[str | None]] = {}
        self._namespace_tokens: dict[str, Token[tuple[str, ...]]] = {}

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Record a real Agent graph step before its body executes."""

        del inputs, kwargs
        step_name = _optional_text(name)
        if step_name is None and isinstance(serialized, Mapping):
            step_name = _optional_text(serialized.get("name"))
        step_name = step_name or "Agent step"
        call_id = str(run_id)
        if call_id in self._steps:
            raise ValueError("Agent step callback run ID started more than once")
        state = self._step_state(
            name=step_name,
            parent_run_id=parent_run_id,
            metadata=metadata,
        )
        self._steps[call_id] = state
        if state.task_id is not None:
            self._graph_tasks[(state.namespace, state.task_id)] = call_id
        if state.step_kind in {"agent", "subagent"}:
            self._agents_by_namespace[state.namespace] = call_id
        await self._observe_step(call_id, state, phase="started")
        await self._hub.force(ObservationBoundary.CALL_STARTED)

    async def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one successful Agent graph step without retaining its state payload."""

        del outputs, parent_run_id, kwargs
        await self._close_step(str(run_id), phase="completed")

    async def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one failed or cancelled Agent graph step."""

        del parent_run_id, kwargs
        if _settles_with_run(error):
            return
        control_phase = _control_flow_phase(error)
        if control_phase is not None:
            await self._close_step(str(run_id), phase=control_phase)
            return
        await self._close_step(
            str(run_id),
            phase="failed",
            error_type=_qualified_name(error),
            error_message=_error_message(error),
            failure_origin=self._claim_error(error),
        )

    def _step_state(
        self,
        *,
        name: str,
        parent_run_id: UUID | None,
        metadata: Mapping[str, object] | None,
    ) -> _AgentStepState:
        segments = _checkpoint_segments(metadata)
        namespace = segments[:-1]
        task_id = _callback_task_id(metadata)
        # LangGraph repeats the current task ID on nested chain callbacks. Only the
        # first chain in that task represents the Native task itself; retaining the ID
        # on descendants would collapse distinct callback nodes into one Graph node.
        if task_id is not None and (namespace, task_id) in self._graph_tasks:
            task_id = None
        raw_parent = _parent_id(parent_run_id)
        middleware_name, separator, hook = name.rpartition(".")
        is_middleware = separator and hook in _MIDDLEWARE_NODE_HOOKS
        parent_tool = None if raw_parent is None else self._tools.get(raw_parent)
        is_subagent = (
            bool(segments)
            and name not in {"model", "tools"}
            and not is_middleware
            and parent_tool is not None
            and parent_tool.tool_name == "task"
        )
        if not segments and raw_parent is None:
            step_kind: Literal[
                "agent", "middleware", "model", "tools", "subagent", "task"
            ] = "agent"
            namespace = ()
            parent_call_id = None
        elif is_subagent:
            step_kind = "subagent"
            namespace = segments
            parent_call_id = raw_parent
        elif is_middleware:
            step_kind = "middleware"
            parent_call_id = self._known_step_parent(raw_parent, namespace)
        elif name == "model":
            step_kind = "model"
            parent_call_id = self._known_step_parent(raw_parent, namespace)
        elif name == "tools":
            step_kind = "tools"
            parent_call_id = self._known_step_parent(raw_parent, namespace)
        else:
            step_kind = "task"
            parent_call_id = self._known_step_parent(raw_parent, namespace)
        return _AgentStepState(
            parent_call_id=parent_call_id,
            namespace=namespace,
            agent_name=(name if step_kind == "subagent" else _agent_name(metadata)),
            step_kind=step_kind,
            name=name,
            task_id=task_id,
            middleware_name=middleware_name if step_kind == "middleware" else None,
            hook=hook if step_kind == "middleware" else None,
        )

    def _known_step_parent(
        self,
        raw_parent: str | None,
        namespace: tuple[str, ...],
    ) -> str | None:
        if raw_parent in self._steps:
            return raw_parent
        return self._agents_by_namespace.get(namespace)

    def _call_parent(
        self,
        parent_run_id: UUID | None,
        metadata: Mapping[str, object] | None,
    ) -> str | None:
        raw_parent = _parent_id(parent_run_id)
        if raw_parent in self._steps:
            return raw_parent
        task_id = _callback_task_id(metadata)
        if task_id is None:
            return raw_parent
        return self._graph_tasks.get(
            (_callback_namespace(metadata), task_id), raw_parent
        )

    async def _observe_step(
        self,
        call_id: str,
        state: _AgentStepState,
        *,
        phase: Literal[
            "started",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "abandoned",
        ],
        error_type: str | None = None,
        error_message: str | None = None,
        failure_origin: bool = False,
    ) -> None:
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            AgentStepObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=phase,
                call_id=call_id,
                parent_call_id=state.parent_call_id,
                namespace=state.namespace,
                agent_name=state.agent_name,
                step_kind=state.step_kind,
                name=state.name,
                task_id=state.task_id,
                middleware_name=state.middleware_name,
                hook=state.hook,
                error_type=error_type,
                error_message=error_message,
                failure_origin=failure_origin,
            )
        )

    async def _close_step(
        self,
        call_id: str,
        *,
        phase: _CallTerminalPhase,
        error_type: str | None = None,
        error_message: str | None = None,
        failure_origin: bool = False,
    ) -> None:
        state = self._steps.get(call_id)
        if state is None:
            return
        await self._observe_step(
            call_id,
            state,
            phase=phase,
            error_type=error_type,
            error_message=error_message,
            failure_origin=failure_origin,
        )
        self._steps.pop(call_id, None)
        if state.task_id is not None:
            key = (state.namespace, state.task_id)
            if self._graph_tasks.get(key) == call_id:
                self._graph_tasks.pop(key, None)
        if self._agents_by_namespace.get(state.namespace) == call_id:
            self._agents_by_namespace.pop(state.namespace, None)

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist the final middleware-processed request before provider execution."""

        del serialized
        if len(messages) != 1:
            raise ValueError(
                "managed model callbacks require exactly one request batch"
            )
        raw_invocation: object = kwargs.get("invocation_params")
        invocation_map: Mapping[str, object] = (
            cast(Mapping[str, object], raw_invocation)
            if isinstance(raw_invocation, Mapping)
            else dict[str, object]()
        )
        raw_options: object = kwargs.get("options")
        provider, model = _provider_and_model(metadata, invocation_map)
        call_id = str(run_id)
        state = _ModelCallState(
            parent_call_id=self._call_parent(parent_run_id, metadata),
            namespace=_callback_namespace(metadata),
            agent_name=_agent_name(metadata),
        )
        observed_at, monotonic_ns = _stamp()
        observation = ModelCallObservation(
            identity=self._hub.context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="started",
            call_id=call_id,
            parent_call_id=state.parent_call_id,
            namespace=state.namespace,
            agent_name=state.agent_name,
            provider=provider,
            model=model,
            messages=tuple(_message_record(message) for message in messages[0]),
            invocation=_source_value(cast(object, raw_invocation)),
            options=_source_value(raw_options),
            output_message_ids=(),
        )
        if call_id in self._models:
            raise ValueError("model callback run ID started more than once")
        self._models[call_id] = state
        await self._hub.observe(observation)
        await self._hub.force(ObservationBoundary.CALL_STARTED)
        self._call_tokens[call_id] = _CURRENT_CALL_ID.set(call_id)
        self._namespace_tokens[call_id] = _CURRENT_NAMESPACE.set(state.namespace)

    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record only the first provider output timestamp, never token content."""

        del token, parent_run_id
        await self._observe_first_model_output(
            str(run_id),
            output_message_ids=_chunk_message_ids(kwargs.get("chunk")),
        )

    async def on_stream_event(
        self,
        event: Mapping[str, object],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the first v3 message boundary without retaining event content."""

        del parent_run_id, kwargs
        if event.get("event") != "message-start":
            return
        message_id = _optional_text(event.get("id"))
        if message_id is None:
            raise TinkerFinStreamProtocolError(
                "v3 message start requires a stable message ID"
            )
        await self._observe_first_model_output(
            str(run_id),
            output_message_ids=(message_id,),
        )

    async def _observe_first_model_output(
        self,
        call_id: str,
        *,
        output_message_ids: tuple[str, ...],
    ) -> None:
        """Emit one provider first-output fact across supported callback surfaces."""

        state = self._models.get(call_id)
        if state is None or call_id in self._first_outputs:
            return
        self._first_outputs.add(call_id)
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            ModelCallObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="first_output",
                call_id=call_id,
                parent_call_id=state.parent_call_id,
                namespace=state.namespace,
                agent_name=state.agent_name,
                output_message_ids=output_message_ids,
            )
        )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one successful provider call with bounded usage metadata."""

        del parent_run_id, kwargs
        usage, response_metadata = _response_values(response)
        await self._close_model(
            str(run_id),
            phase="completed",
            usage=usage,
            response_metadata=response_metadata,
            output_message_ids=_response_message_ids(response),
            tool_call_ids=_response_tool_call_ids(response),
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one failed or cancelled provider call without swallowing cancellation."""

        del parent_run_id, kwargs
        if _settles_with_run(error):
            return
        control_phase = _control_flow_phase(error)
        if control_phase is not None:
            await self._close_model(str(run_id), phase=control_phase)
            return
        failure_origin = self._claim_error(error)
        await self._close_model(
            str(run_id),
            phase="failed",
            error_type=_qualified_name(error),
            error_message=_error_message(error),
            failure_origin=failure_origin,
        )

    async def _close_model(
        self,
        call_id: str,
        *,
        phase: _ModelTerminalPhase,
        usage: dict[str, JsonValue] | None = None,
        response_metadata: dict[str, JsonValue] | None = None,
        output_message_ids: tuple[str, ...] = (),
        tool_call_ids: tuple[str, ...] = (),
        error_type: str | None = None,
        error_message: str | None = None,
        failure_origin: bool = False,
    ) -> None:
        state = self._models.get(call_id)
        if state is None:
            return
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            ModelCallObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=phase,
                call_id=call_id,
                parent_call_id=state.parent_call_id,
                namespace=state.namespace,
                agent_name=state.agent_name,
                usage=usage,
                response_metadata=response_metadata,
                output_message_ids=output_message_ids,
                tool_call_ids=tool_call_ids,
                error_type=error_type,
                error_message=error_message,
                failure_origin=failure_origin,
            )
        )
        self._models.pop(call_id, None)
        self._first_outputs.discard(call_id)
        self._reset_call_token(call_id)

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist the actual Tool input after approval and middleware editing."""

        raw_name = serialized.get("name")
        tool_name = _optional_text(raw_name)
        if tool_name is None:
            raise ValueError("Tool callback requires a canonical name")
        raw_tool_call_id = kwargs.get("tool_call_id")
        tool_call_id = _optional_text(raw_tool_call_id)
        execution_id = str(run_id)
        state = _ToolCallState(
            parent_call_id=self._call_parent(parent_run_id, metadata),
            namespace=_callback_namespace(metadata),
            agent_name=_agent_name(metadata),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        actual_input: object = inputs if inputs is not None else input_str
        observed_at, monotonic_ns = _stamp()
        observation = ToolExecutionObservation(
            identity=self._hub.context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="started",
            execution_id=execution_id,
            parent_call_id=state.parent_call_id,
            namespace=state.namespace,
            agent_name=state.agent_name,
            tool_call_id=state.tool_call_id,
            tool_name=state.tool_name,
            input=_source_value(actual_input),
        )
        if execution_id in self._tools:
            raise ValueError("Tool callback run ID started more than once")
        self._tools[execution_id] = state
        await self._hub.observe(observation)
        await self._hub.force(ObservationBoundary.CALL_STARTED)
        self._call_tokens[execution_id] = _CURRENT_CALL_ID.set(execution_id)
        self._namespace_tokens[execution_id] = _CURRENT_NAMESPACE.set(state.namespace)

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one Tool execution, including handled error Tool messages."""

        del parent_run_id, kwargs
        handled_error = isinstance(output, ToolMessage) and output.status == "error"
        await self._close_tool(
            str(run_id),
            phase="failed" if handled_error else "completed",
            output=_source_value(output),
            failure_origin=handled_error,
        )

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Close one Tool exception while preserving its real error classification."""

        del parent_run_id, kwargs
        if _settles_with_run(error):
            return
        control_phase = _control_flow_phase(error)
        if control_phase is not None:
            await self._close_tool(str(run_id), phase=control_phase)
            return
        failure_origin = self._claim_error(error)
        await self._close_tool(
            str(run_id),
            phase="failed",
            error_type=_qualified_name(error),
            error_message=_error_message(error),
            failure_origin=failure_origin,
        )

    async def _close_tool(
        self,
        execution_id: str,
        *,
        phase: _ToolTerminalPhase,
        output: JsonValue | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        failure_origin: bool = False,
    ) -> None:
        state = self._tools.get(execution_id)
        if state is None:
            return
        observed_at, monotonic_ns = _stamp()
        await self._hub.observe(
            ToolExecutionObservation(
                identity=self._hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=phase,
                execution_id=execution_id,
                parent_call_id=state.parent_call_id,
                namespace=state.namespace,
                agent_name=state.agent_name,
                tool_call_id=state.tool_call_id,
                tool_name=state.tool_name,
                output=output,
                error_type=error_type,
                error_message=error_message,
                failure_origin=failure_origin,
            )
        )
        self._tools.pop(execution_id, None)
        self._reset_call_token(execution_id)

    async def settle(
        self,
        outcome: RunTerminalOutcome,
        *,
        error: BaseException | None,
    ) -> None:
        """Close callbacks that upstream cancellation or failure left unmatched."""

        del error
        phase: Literal["cancelled", "interrupted", "abandoned"]
        if outcome == "cancelled":
            phase = "cancelled"
        elif outcome == "interrupted":
            phase = "interrupted"
        else:
            phase = "abandoned"
        models = tuple(self._models.items())
        tools = tuple(self._tools.items())
        steps = tuple(reversed(tuple(self._steps.items())))
        self._models.clear()
        self._tools.clear()
        self._steps.clear()
        self._graph_tasks.clear()
        self._agents_by_namespace.clear()
        self._first_outputs.clear()
        for call_id, state in models:
            observed_at, monotonic_ns = _stamp()
            await self._hub.observe(
                ModelCallObservation(
                    identity=self._hub.context.identity,
                    observed_at=observed_at,
                    monotonic_ns=monotonic_ns,
                    phase=phase,
                    call_id=call_id,
                    parent_call_id=state.parent_call_id,
                    namespace=state.namespace,
                    agent_name=state.agent_name,
                    output_message_ids=(),
                )
            )
        for execution_id, state in tools:
            observed_at, monotonic_ns = _stamp()
            await self._hub.observe(
                ToolExecutionObservation(
                    identity=self._hub.context.identity,
                    observed_at=observed_at,
                    monotonic_ns=monotonic_ns,
                    phase=phase,
                    execution_id=execution_id,
                    parent_call_id=state.parent_call_id,
                    namespace=state.namespace,
                    agent_name=state.agent_name,
                    tool_call_id=state.tool_call_id,
                    tool_name=state.tool_name,
                )
            )
        for call_id, state in steps:
            await self._observe_step(
                call_id,
                state,
                phase=phase,
            )
        self._call_tokens.clear()
        self._namespace_tokens.clear()

    def _claim_error(self, error: BaseException) -> bool:
        """Return whether this callback is the first owner of one propagated failure."""

        return self._hub.claim_error(error)

    def _reset_call_token(self, call_id: str) -> None:
        token = self._call_tokens.pop(call_id, None)
        namespace_token = self._namespace_tokens.pop(call_id, None)
        if token is not None:
            try:
                _CURRENT_CALL_ID.reset(token)
            except ValueError:
                # Some providers finish callbacks from their shielded producer task.
                # That task owns its copied context and exits after the callback.
                pass
        if namespace_token is not None:
            try:
                _CURRENT_NAMESPACE.reset(namespace_token)
            except ValueError:
                pass


class TraceContribution:
    """Collect an optional semantic result for one explicit contribution scope."""

    __slots__ = ("_result", "_result_set")

    def __init__(self) -> None:
        self._result: object = None
        self._result_set = False

    def set_result(self, result: object) -> None:
        """Attach a result captured when the contribution completes successfully."""

        self._result = result
        self._result_set = True


@asynccontextmanager
async def trace_contribution(
    *,
    kind: ContextKind,
    name: str,
    input: object = None,
) -> AsyncGenerator[TraceContribution, None]:
    """Expose one product-semantic context action to active Runtime observers.

    The scope is a no-op when called outside a managed observed Run. This lets reusable
    middleware and Tools publish Memory, Guardrail, retrieval, or custom semantics
    without depending on a Tracer implementation or checking host configuration.

    Args:
        kind: Stable functional category shown to Trace consumers.
        name: User-facing action name.
        input: Optional public input subject to each Observer's capture policy.

    Yields:
        A contribution value whose optional result is recorded on success.

    Raises:
        TypeError: ``kind`` or ``name`` has the wrong type.
        ValueError: ``name`` is blank or not canonical.
        BaseException: The contribution body or active Observer fails.
    """

    if kind not in {"memory", "guardrail", "retrieval", "custom"}:
        raise ValueError("kind must be memory, guardrail, retrieval, or custom")
    if not isinstance(name, str):
        raise TypeError("name must be text")
    if not name or name != name.strip():
        raise ValueError("name must be canonical non-empty text")
    value = TraceContribution()
    hub = _CURRENT_HUB.get()
    if hub is None or not hub.enabled:
        yield value
        return
    contribution_id = uuid4().hex
    parent_call_id = _CURRENT_CALL_ID.get()
    namespace = _CURRENT_NAMESPACE.get()
    observed_at, monotonic_ns = _stamp()
    await hub.observe(
        ContextContributionObservation(
            identity=hub.context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            phase="started",
            contribution_id=contribution_id,
            parent_call_id=parent_call_id,
            namespace=namespace,
            context_kind=kind,
            name=name,
            input=None if input is None else _source_value(input),
        )
    )
    try:
        yield value
    except BaseException as error:
        observed_at, monotonic_ns = _stamp()
        control_phase = _control_flow_phase(error)
        settles_with_run = _settles_with_run(error)
        await hub.observe(
            ContextContributionObservation(
                identity=hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase=(
                    "abandoned" if settles_with_run else (control_phase or "failed")
                ),
                contribution_id=contribution_id,
                parent_call_id=parent_call_id,
                namespace=namespace,
                context_kind=kind,
                name=name,
                error_type=(
                    None
                    if control_phase is not None or settles_with_run
                    else _qualified_name(error)
                ),
                failure_origin=(
                    control_phase is None
                    and not settles_with_run
                    and hub.claim_error(error)
                ),
            )
        )
        raise
    else:
        observed_at, monotonic_ns = _stamp()
        await hub.observe(
            ContextContributionObservation(
                identity=hub.context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="completed",
                contribution_id=contribution_id,
                parent_call_id=parent_call_id,
                namespace=namespace,
                context_kind=kind,
                name=name,
                output=_source_value(value._result) if value._result_set else None,
            )
        )


__all__ = [
    "RuntimeCallHandler",
    "TraceContribution",
    "bind_observation_hub",
    "reset_observation_hub",
    "trace_contribution",
]
