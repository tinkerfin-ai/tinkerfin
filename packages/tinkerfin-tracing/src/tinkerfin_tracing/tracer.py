"""Runtime Observer that records normalized semantic facts into a Trace Store."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import BaseModel, JsonValue

from tinkerfin_contracts import (
    AgentStepObservation,
    ContextContributionObservation,
    ModelCallObservation,
    NativeExtraObservation,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeTaskObservation,
    NativeToolCall,
    ObservationBoundary,
    RunClosedObservation,
    RunInputObservation,
    RunObservationSession,
    RunObserverFailedObservation,
    RunResumeCheckpointedObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RuntimeObservation,
    ToolExecutionObservation,
)

from ._entry_projection import project_trace_entry
from ._ids import scope_id as _scope_id
from .capture import CapturedValue, CapturePolicy, ReasoningCapturePolicy
from .durable_store import InMemoryTraceStore
from .entries import (
    TraceEntry,
    TraceEntryCompleteness,
    TraceEntryPage,
    TraceFilter,
    TraceTurn,
)
from .entry_query import TraceQuery, decode_entry_cursor, encode_entry_cursor
from .errors import TraceCaptureRejected, TraceCorruption, TraceStoreProtocolError
from .facts import (
    AgentStepFact,
    CallTrackingFact,
    ContextContributionFact,
    InteractionFact,
    MessageFact,
    MiddlewareFact,
    ModelCallFact,
    NativeExtraFact,
    PlanRevisionFact,
    ReasoningFact,
    RunFact,
    RuntimeTaskFact,
    SkillFact,
    StateRevisionFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TraceFactBase,
    TraceSemanticFact,
    TurnFact,
)
from .limits import TraceLimits
from .projection import (
    CoreProjectionState,
    CoreProjectionWindow,
    RegisteredTraceProjection,
    TraceProjection,
    project_core_checkpoint,
    select_core_projection_window,
    select_prior_run_ids,
)
from .query import (
    TraceThread,
    build_trace_thread,
    load_core_projection_state,
    read_lineage_events,
    resolve_history_request,
)
from .store import (
    StoreThreadSnapshot,
    TraceEntryRebuildStore,
    TraceEntryStore,
    TraceStore,
    TraceThreadKey,
    TraceWriter,
)
from .writing import TraceBatchWriter, TraceWritePolicy


def _fingerprint(value: JsonValue) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _message_role(message: NativeMessageRecord) -> str:
    return {
        "human": "user",
        "assistant": "assistant",
        "assistant_chunk": "assistant",
        "tool": "tool",
        "system": "system",
    }.get(message.message_type, "other")


def _make_fact(
    fact_type: type[TraceFactBase],
    common: Mapping[str, object],
    **values: object,
) -> TraceSemanticFact:
    """Validate a fact without weakening constructor types through kwargs maps."""

    return cast(
        TraceSemanticFact,
        fact_type.model_validate({**common, **values}),
    )


@dataclass(frozen=True, slots=True)
class _PendingInteraction:
    """Retain exact review correlation until resolution or session hydration."""

    kind: str
    tool_call_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SubagentDescriptor:
    """Retain one proven Deep Agents task Tool to child-namespace relationship."""

    parent_tool_call_id: str
    parent_task_id: str
    agent_name: str
    description: str


@dataclass(frozen=True, slots=True)
class _PendingToolExecution:
    """Retain actual Tool input until its execution callback reaches a terminal."""

    input: JsonValue | None
    parent_call_id: str | None
    namespace: tuple[str, ...]
    agent_name: str | None
    tool_name: str
    traced: bool


@dataclass(frozen=True, slots=True)
class _PendingRuntimeTask:
    """Retain one Native task identity until its result or Run terminal."""

    namespace: tuple[str, ...]
    run_id: str
    source_task_id: str
    task_name: str


class _TracingSession:
    """Own one Run writer and its ordered, bounded semantic mapping lifecycle.

    Observations arrive serially from one Runtime session. The session owns the
    ``TraceBatchWriter`` worker but only borrows the Store and writer contract returned
    by that Store. Dedupe, Tool fragments, state baselines, and pending interactions are
    advanced only after facts are accepted in order. Terminal and close facts use the
    Store's mandatory reserve after ordinary reasoning completion is forced separately.
    Any worker failure becomes the active Observer failure and is never hidden by close.
    """

    def __init__(
        self,
        *,
        writer: TraceWriter,
        store: TraceStore,
        context: RunSourceContext,
        capture_policy: CapturePolicy,
        reasoning_capture_policy: ReasoningCapturePolicy,
        limits: TraceLimits,
        write_policy: TraceWritePolicy,
        on_closed: Callable[[_TracingSession], None],
        prior_events: tuple[TraceEvent, ...] = (),
    ) -> None:
        """Initialize one request session from the current lineage prefix.

        Args:
            writer: Run-exclusive Store writer transferred to this session.
            store: Borrowed Store used for projection checkpoint advancement.
            context: Immutable Runtime input and privacy facts for this Run.
            capture_policy: Public semantic payload capture policy.
            reasoning_capture_policy: Independent provider reasoning retention policy.
            limits: Store-aligned event and payload capacity limits.
            write_policy: Batching, pending-byte, backpressure, and delay policy.
            on_closed: Callback that removes this session from its Tracer owner.
            prior_events: Selected lineage facts used only to hydrate dedupe state.

        Raises:
            TraceCorruption: Prior facts cannot hydrate one deterministic state.
        """

        self._writer = writer
        self._store = store
        self._context = context
        self._policy = capture_policy
        self._reasoning_policy = reasoning_capture_policy
        self._middleware_descriptors = {
            descriptor.name: descriptor for descriptor in context.middleware
        }
        self._configured_middleware: set[str] = set()
        self._limits = limits
        self._on_closed = on_closed
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._observation_index = 0
        self._run_scope = hashlib.sha256(writer.run_id.encode("utf-8")).hexdigest()
        self._private_state_keys = frozenset(context.private_state_keys)
        self._message_fingerprints: dict[tuple[tuple[str, ...], str], str] = {}
        self._message_seen: set[tuple[tuple[str, ...], str]] = set()
        self._message_contents: dict[tuple[tuple[str, ...], str], JsonValue] = {}
        self._message_completed: set[tuple[tuple[str, ...], str]] = set()
        self._reasoning_contents: dict[tuple[tuple[str, ...], str], JsonValue] = {}
        self._reasoning_extractors: dict[tuple[tuple[str, ...], str], str] = {}
        self._reasoning_completed: set[tuple[tuple[str, ...], str]] = set()
        self._active_reasoning: dict[tuple[tuple[str, ...], str], None] = {}
        self._tool_slots: dict[tuple[tuple[str, ...], str, int], tuple[str, str]] = {}
        self._tool_names: dict[tuple[tuple[str, ...], str], str] = {}
        self._tool_started: set[tuple[tuple[str, ...], str]] = set()
        self._tool_argument_fragments: dict[tuple[tuple[str, ...], str], str] = {}
        self._tool_argument_snapshots: set[tuple[tuple[str, ...], str]] = set()
        self._tool_completed: set[tuple[tuple[str, ...], str]] = set()
        self._tool_results: set[tuple[tuple[str, ...], str]] = set()
        self._tool_call_models: dict[tuple[tuple[str, ...], str], str] = {}
        self._tool_executions: dict[str, _PendingToolExecution] = {}
        self._tool_execution_by_call: dict[tuple[tuple[str, ...], str], str] = {}
        self._visible_tool_execution_calls: set[tuple[tuple[str, ...], str]] = set()
        self._active_runtime_tasks: dict[
            tuple[tuple[str, ...], str], _PendingRuntimeTask
        ] = {}
        self._callback_entries: dict[str, str] = {}
        self._states: dict[tuple[str, ...], dict[str, JsonValue]] = {}
        self._pending_interactions: dict[
            tuple[tuple[str, ...], str], _PendingInteraction
        ] = {}
        self._subagent_descriptors: dict[tuple[str, ...], _SubagentDescriptor] = {}
        self._active_subagents: dict[tuple[str, ...], tuple[str, str | None]] = {}
        self._failure_origin_seen = False
        self._batch_writer = TraceBatchWriter(
            writer,
            policy=write_policy,
            on_committed=self._checkpoint_committed,
        )
        self._hydrate(prior_events)

    def _hydrate(self, events: tuple[TraceEvent, ...]) -> None:
        """Restore dedupe and state baselines from the current semantic Ledger."""

        for event in events:
            fact = event.fact
            if isinstance(fact, MessageFact) and fact.source_message_id is not None:
                key = (fact.namespace, fact.source_message_id)
                self._message_seen.add(key)
                if fact.phase == "removed":
                    self._message_seen.discard(key)
                    self._message_fingerprints.pop(key, None)
                    self._message_contents.pop(key, None)
                    self._message_completed.discard(key)
                    continue
                if fact.content is not None:
                    self._track_message_content(
                        key,
                        fact.content,
                        append=fact.phase == "content",
                    )
                if fact.phase in {"completed", "reconciled"}:
                    self._message_completed.add(key)
                if fact.fingerprint is not None:
                    self._message_fingerprints[key] = fact.fingerprint
            elif isinstance(fact, ReasoningFact):
                key = (fact.namespace, fact.source_message_id)
                previous_extractor = self._reasoning_extractors.get(key)
                if (
                    previous_extractor is not None
                    and previous_extractor != fact.extractor
                ):
                    raise TraceCorruption(
                        "Reasoning extractor changed inside one scoped message"
                    )
                self._reasoning_extractors[key] = fact.extractor
                if fact.content is not None:
                    self._track_reasoning_content(
                        key,
                        fact.content,
                        append=fact.phase == "content",
                    )
                if fact.phase == "completed":
                    self._reasoning_completed.add(key)
            elif isinstance(fact, ToolFact):
                key = (fact.namespace, fact.source_tool_call_id)
                self._tool_names[key] = fact.tool_name
                if fact.phase == "started":
                    self._tool_started.add(key)
                if fact.phase == "arguments" and fact.content is not None:
                    self._tool_argument_snapshots.add(key)
                if fact.phase in {"completed", "result", "cancelled", "abandoned"}:
                    self._tool_completed.add(key)
                if fact.phase in {"result", "cancelled", "abandoned"}:
                    self._tool_results.add(key)
            elif isinstance(fact, ModelCallFact) and fact.phase == "completed":
                for tool_call_id in fact.tool_call_ids:
                    self._tool_call_models[(fact.namespace, tool_call_id)] = (
                        fact.call_id
                    )
            elif (
                isinstance(fact, ToolExecutionFact)
                and fact.phase == "started"
                and fact.source_tool_call_id is not None
            ):
                self._tool_execution_by_call[
                    (fact.namespace, fact.source_tool_call_id)
                ] = fact.execution_id
            elif isinstance(fact, RuntimeTaskFact):
                key = (fact.namespace, fact.source_task_id)
                if fact.phase in {"started", "interrupted"}:
                    self._active_runtime_tasks[key] = _PendingRuntimeTask(
                        namespace=fact.namespace,
                        run_id=fact.identity.run_id,
                        source_task_id=fact.source_task_id,
                        task_name=fact.task_name,
                    )
                else:
                    self._active_runtime_tasks.pop(key, None)
            elif isinstance(fact, StateRevisionFact):
                state = self._states.setdefault(fact.namespace, {})
                for key in fact.removed_keys:
                    state.pop(key, None)
                if fact.changes.disposition == "inline" and isinstance(
                    fact.changes.value, dict
                ):
                    state.update(fact.changes.value)
            elif isinstance(fact, PlanRevisionFact):
                state = self._states.setdefault(fact.namespace, {})
                if fact.plan.disposition == "inline":
                    state["tinkerfin_plan"] = fact.plan.value
            elif isinstance(fact, InteractionFact):
                key = (fact.namespace, fact.source_interaction_id)
                if fact.phase == "opened":
                    self._pending_interactions[key] = _PendingInteraction(
                        kind=fact.interaction_kind,
                        tool_call_ids=fact.tool_call_ids,
                    )
                else:
                    self._pending_interactions.pop(key, None)
            elif isinstance(fact, SubagentFact):
                if fact.phase in {"started", "updated"} and fact.status in {
                    "running",
                    "waiting",
                }:
                    self._active_subagents[fact.namespace] = (
                        fact.subagent_id,
                        fact.agent_name,
                    )
                else:
                    self._active_subagents.pop(fact.namespace, None)
            elif isinstance(fact, MiddlewareFact):
                self._configured_middleware.add(fact.name)

    async def observe(self, observation: RuntimeObservation) -> None:
        """Map and enqueue one already ordered Runtime observation.

        Args:
            observation: Validated lifecycle or Native semantic observation.

        Raises:
            RuntimeError: The request session is already closed.
            TraceCorruption: Observation order or correlation is inconsistent.
            TraceObserverFailed: The bounded writer or Store commit failed.
        """

        if self._closed:
            raise RuntimeError("Trace observation session is closed")
        self._observation_index += 1
        source_id = (
            f"observation:{self._writer.key.generation}:{self._run_scope}:"
            f"{self._observation_index}"
        )
        if isinstance(observation, (RunTerminalObservation, RunClosedObservation)):
            common = {
                "source_observation_id": source_id,
                "identity": observation.identity,
                "occurred_at": observation.observed_at,
                "monotonic_ns": observation.monotonic_ns,
            }
            completions = self._complete_open_reasoning(common)
            if completions:
                await self._append(tuple(completions), mandatory=False)
        facts = self._facts(observation, source_id=source_id)
        if not facts:
            return
        if isinstance(observation, RunTerminalObservation):
            settlement = tuple(facts[:-1])
            if settlement:
                await self._append(settlement, mandatory=False)
            await self._append((facts[-1],), mandatory=True)
            return
        mandatory = isinstance(
            observation, (RunTerminalObservation, RunClosedObservation)
        )
        await self._append(tuple(facts), mandatory=mandatory)
        if isinstance(observation, RunInputObservation):
            await self._batch_writer.force()

    async def _append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool,
    ) -> None:
        """Submit facts to the bounded writer; mandatory calls force prior work."""

        await self._batch_writer.submit(facts, mandatory=mandatory)

    async def _checkpoint_committed(
        self,
        events: tuple[TraceEvent, ...],
    ) -> None:
        """Advance framework-owned core cache after one Store transaction commits."""

        await load_core_projection_state(
            self._store,
            self._writer.key,
            as_of_seq=events[-1].trace_seq,
        )

    async def force(self, boundary: ObservationBoundary) -> None:
        """Commit all accepted facts at a Runtime hard boundary.

        Args:
            boundary: Runtime reason for the flush. All current boundary kinds share the
                same commit guarantee; the value remains available for observability.

        Raises:
            RuntimeError: The request session is already closed.
            TraceObserverFailed: A pending or forced Store transaction failed.
        """

        del boundary
        if self._closed:
            raise RuntimeError("Trace observation session is closed")
        await self._batch_writer.force()

    async def flush(self) -> None:
        """Commit every locally accepted fact before a same-Tracer query snapshot."""

        if not self._closed:
            await self._batch_writer.force()

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        """Return the session-owned future that completes on background failure."""

        return self._batch_writer.failure_waiter()

    async def aclose(self) -> None:
        """Settle accepted writes, close the writer once, and release Tracer ownership.

        Repeated calls are idempotent. Cancellation and the first writer failure retain
        precedence over secondary cleanup errors.

        Raises:
            BaseException: Cancellation or the primary batching/Store failure.
        """

        async with self._close_lock:
            if self._closed:
                return
            try:
                await self._batch_writer.aclose()
            finally:
                self._closed = True
                self._on_closed(self)

    def _facts(
        self,
        observation: RuntimeObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        if isinstance(observation, RunStartedObservation):
            return [
                _make_fact(
                    RunFact,
                    common,
                    phase="started",
                    input_kind=self._context.input_kind,
                    parent_run_id=self._context.parent_run_id,
                )
            ]
        if isinstance(observation, RunInputObservation):
            source = observation.source
            facts: list[TraceSemanticFact] = []
            public_input = _discard_private_state(
                source.input,
                self._private_state_keys,
            )
            public_config = _discard_private_state(
                source.config,
                self._private_state_keys,
            )
            user_message = _user_message(public_input)
            implicit_resume_ids: tuple[str, ...] = ()
            if (
                source.input_kind == "resume"
                and not source.resume
                and len(self._pending_interactions) == 1
            ):
                implicit_resume_ids = (next(iter(self._pending_interactions))[1],)
            resume_ids = (
                tuple(item.interrupt_id for item in source.resume) + implicit_resume_ids
            )
            if source.input_kind in {"ordinary", "branch"}:
                facts.append(
                    _make_fact(
                        TurnFact,
                        common,
                        turn_id=(
                            f"turn:{self._writer.key.generation}:"
                            f"{observation.identity.run_id}"
                        ),
                        user_message_id=(
                            None if user_message is None else user_message[0]
                        ),
                        parent_run_id=source.parent_run_id,
                    )
                )
            facts.append(
                _make_fact(
                    RunFact,
                    common,
                    phase=(
                        "resumed"
                        if source.input_kind in {"resume", "abandon"}
                        else "input"
                    ),
                    input_kind=source.input_kind,
                    parent_run_id=source.parent_run_id,
                    input=(
                        self._capture_structure(public_input, divisor=2)
                        if source.input_kind in {"ordinary", "branch"}
                        else self._capture(
                            [
                                item.model_dump(mode="json", by_alias=True)
                                for item in source.resume
                            ],
                            divisor=2,
                        )
                    ),
                    config=self._capture_structure(public_config, divisor=2),
                    interrupt_ids=resume_ids,
                )
            )
            if source.call_tracking_enabled:
                facts.append(_make_fact(CallTrackingFact, common))
            if user_message is not None and source.input_kind in {"ordinary", "branch"}:
                user_message_id, user_content = user_message
                scoped_message_id = _scope_id("message", (), user_message_id)
                user_key = ((), user_message_id)
                self._message_seen.add(user_key)
                captured_user_content = self._capture(user_content)
                self._track_message_content(
                    user_key,
                    captured_user_content,
                    append=False,
                )
                self._message_completed.add(user_key)
                facts.append(
                    MessageFact(
                        source_observation_id=source_id,
                        identity=observation.identity,
                        occurred_at=observation.observed_at,
                        monotonic_ns=observation.monotonic_ns,
                        phase="reconciled",
                        message_id=scoped_message_id,
                        source_message_id=user_message_id,
                        role="user",
                        content=captured_user_content,
                    )
                )
            for summary in source.resume:
                (
                    interaction_namespace,
                    pending_interaction,
                ) = self._resolve_pending_interaction(
                    summary.interrupt_id,
                )
                facts.append(
                    _make_fact(
                        InteractionFact,
                        common,
                        phase="resolved",
                        interaction_id=_scope_id(
                            "interaction",
                            interaction_namespace,
                            summary.interrupt_id,
                        ),
                        source_interaction_id=summary.interrupt_id,
                        namespace=interaction_namespace,
                        interaction_kind=pending_interaction.kind,
                        tool_call_ids=pending_interaction.tool_call_ids,
                        status=summary.status,
                        payload=(
                            None
                            if summary.decision is None
                            else self._capture({"decision": summary.decision})
                        ),
                    )
                )
            for interrupt_id in implicit_resume_ids:
                (
                    interaction_namespace,
                    pending_interaction,
                ) = self._resolve_pending_interaction(interrupt_id)
                facts.append(
                    _make_fact(
                        InteractionFact,
                        common,
                        phase="resolved",
                        interaction_id=_scope_id(
                            "interaction",
                            interaction_namespace,
                            interrupt_id,
                        ),
                        source_interaction_id=interrupt_id,
                        namespace=interaction_namespace,
                        interaction_kind=pending_interaction.kind,
                        tool_call_ids=pending_interaction.tool_call_ids,
                        status="resolved",
                    )
                )
            for descriptor in source.middleware:
                middleware_capture = self._policy.middleware_capture(
                    name=descriptor.name,
                    class_name=descriptor.class_name,
                )
                if middleware_capture.mode == "disabled":
                    continue
                self._configured_middleware.add(descriptor.name)
                facts.append(
                    _make_fact(
                        MiddlewareFact,
                        common,
                        middleware_id=_scope_id(
                            "middleware",
                            (),
                            f"{observation.identity.run_id}:{descriptor.name}",
                        ),
                        name=descriptor.name,
                        class_name=descriptor.class_name,
                        hooks=descriptor.hooks,
                    )
                )
            return facts
        if isinstance(observation, AgentStepObservation):
            middleware_configuration: TraceSemanticFact | None = None
            if observation.step_kind == "middleware":
                middleware_name = observation.middleware_name
                if middleware_name is None:  # pragma: no cover - contract validation
                    raise TraceCorruption("middleware Agent step has no name")
                descriptor = self._middleware_descriptors.get(middleware_name)
                class_name = (
                    descriptor.class_name
                    if descriptor is not None
                    else self._policy._middleware_class_name(middleware_name)
                )
                middleware_capture = self._policy.middleware_capture(
                    name=middleware_name,
                    class_name=class_name,
                )
                if (
                    middleware_capture.mode != "disabled"
                    and middleware_name not in self._configured_middleware
                ):
                    self._configured_middleware.add(middleware_name)
                    middleware_configuration = _make_fact(
                        MiddlewareFact,
                        common,
                        middleware_id=_scope_id(
                            "middleware",
                            (),
                            f"{observation.identity.run_id}:{middleware_name}",
                        ),
                        name=middleware_name,
                        class_name=class_name,
                        hooks=(
                            descriptor.hooks
                            if descriptor is not None
                            else (
                                () if observation.hook is None else (observation.hook,)
                            )
                        ),
                    )
                if middleware_capture.mode != "visible":
                    return (
                        []
                        if middleware_configuration is None
                        else [middleware_configuration]
                    )
            if observation.step_kind == "agent":
                call_id = _scope_id(
                    "agent",
                    observation.namespace,
                    observation.identity.run_id,
                )
            elif observation.step_kind == "subagent" and observation.namespace:
                call_id = _scope_id(
                    "subagent",
                    observation.namespace,
                    observation.namespace[-1],
                )
            else:
                call_id = _scope_id(
                    "agent-step",
                    observation.namespace,
                    observation.call_id,
                )
            parent_call_id = (
                None
                if observation.parent_call_id is None
                else self._callback_entries.get(observation.parent_call_id)
            )
            if observation.phase == "started":
                self._callback_entries[observation.call_id] = call_id
            elif observation.phase in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "abandoned",
            }:
                self._callback_entries.pop(observation.call_id, None)
            facts = (
                [] if middleware_configuration is None else [middleware_configuration]
            )
            if observation.failure_origin:
                self._failure_origin_seen = True
            facts.append(
                _make_fact(
                    AgentStepFact,
                    common,
                    namespace=observation.namespace,
                    phase=observation.phase,
                    call_id=call_id,
                    parent_call_id=parent_call_id,
                    step_kind=observation.step_kind,
                    name=observation.name,
                    source_task_id=observation.task_id,
                    agent_name=observation.agent_name,
                    middleware_name=observation.middleware_name,
                    hook=observation.hook,
                    error_type=observation.error_type,
                    error_message=(
                        self._capture(observation.error_message)
                        if self._policy.include_error_messages
                        and observation.error_message is not None
                        else None
                    ),
                    failure_origin=observation.failure_origin,
                )
            )
            return facts
        if isinstance(observation, ModelCallObservation):
            if observation.failure_origin:
                self._failure_origin_seen = True
            call_id = _scope_id(
                "model-call",
                observation.namespace,
                observation.call_id,
            )
            if observation.phase == "started":
                self._callback_entries[observation.call_id] = call_id
            elif observation.phase in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "abandoned",
            }:
                self._callback_entries.pop(observation.call_id, None)
            if observation.phase == "completed":
                for tool_call_id in observation.tool_call_ids:
                    self._tool_call_models[(observation.namespace, tool_call_id)] = (
                        call_id
                    )
            request = (
                None
                if observation.phase != "started"
                else self._capture(
                    {
                        "messages": [
                            message.model_dump(mode="json", by_alias=True)
                            for message in observation.messages
                        ],
                        "invocation": observation.invocation,
                        "options": observation.options,
                    }
                )
            )
            return [
                _make_fact(
                    ModelCallFact,
                    common,
                    namespace=observation.namespace,
                    phase=observation.phase,
                    call_id=call_id,
                    parent_call_id=(
                        None
                        if observation.parent_call_id is None
                        else self._callback_entries.get(observation.parent_call_id)
                    ),
                    agent_name=observation.agent_name,
                    provider=observation.provider,
                    model=observation.model,
                    request=request,
                    usage=(
                        None
                        if observation.usage is None
                        else self._capture(observation.usage)
                    ),
                    response_metadata=(
                        None
                        if observation.response_metadata is None
                        else self._capture(observation.response_metadata)
                    ),
                    tool_call_ids=observation.tool_call_ids,
                    error_type=observation.error_type,
                    error_message=(
                        self._capture(observation.error_message)
                        if self._policy.include_error_messages
                        and observation.error_message is not None
                        else None
                    ),
                    failure_origin=observation.failure_origin,
                )
            ]
        if isinstance(observation, ToolExecutionObservation):
            execution_id = _scope_id(
                "tool-execution",
                observation.namespace,
                observation.execution_id,
            )
            if observation.phase == "started":
                if observation.execution_id in self._tool_executions:
                    raise TraceCorruption("Tool execution started more than once")
                if observation.input is None:  # pragma: no cover - contract validation
                    raise TraceCorruption("Tool execution start has no input")
                if observation.tool_call_id is not None:
                    self._tool_execution_by_call[
                        (observation.namespace, observation.tool_call_id)
                    ] = execution_id
                traced = self._policy.traces_tool(observation.tool_name)
                if traced and observation.tool_call_id is not None:
                    self._visible_tool_execution_calls.add(
                        (observation.namespace, observation.tool_call_id)
                    )
                parent_call_id = (
                    None
                    if observation.parent_call_id is None
                    else self._callback_entries.get(observation.parent_call_id)
                )
                self._tool_executions[observation.execution_id] = _PendingToolExecution(
                    input=observation.input if traced else None,
                    parent_call_id=parent_call_id,
                    namespace=observation.namespace,
                    agent_name=observation.agent_name,
                    tool_name=observation.tool_name,
                    traced=traced,
                )
                if not traced:
                    return []
                self._callback_entries[observation.execution_id] = execution_id
                return [
                    _make_fact(
                        ToolExecutionFact,
                        common,
                        namespace=observation.namespace,
                        phase="started",
                        execution_id=execution_id,
                        parent_call_id=parent_call_id,
                        agent_name=observation.agent_name,
                        source_tool_call_id=observation.tool_call_id,
                        tool_name=observation.tool_name,
                        input=self._policy.capture_tool(
                            tool_name=observation.tool_name,
                            value=observation.input,
                            target="arguments",
                            max_bytes=self._payload_budget,
                        ),
                    )
                ]
            pending = self._tool_executions.pop(observation.execution_id, None)
            self._callback_entries.pop(observation.execution_id, None)
            if pending is None:
                raise TraceCorruption("Tool execution terminal has no matching start")
            if (
                pending.namespace != observation.namespace
                or pending.agent_name != observation.agent_name
                or pending.tool_name != observation.tool_name
            ):
                raise TraceCorruption("Tool execution identity changed before terminal")
            if not pending.traced:
                return []
            if observation.failure_origin:
                self._failure_origin_seen = True
            output = (
                None
                if observation.output is None
                else self._policy.capture_tool(
                    tool_name=observation.tool_name,
                    value=observation.output,
                    target="result",
                    max_bytes=self._payload_budget,
                )
            )
            facts = [
                _make_fact(
                    ToolExecutionFact,
                    common,
                    namespace=observation.namespace,
                    phase=observation.phase,
                    execution_id=execution_id,
                    parent_call_id=pending.parent_call_id,
                    agent_name=observation.agent_name,
                    source_tool_call_id=observation.tool_call_id,
                    tool_name=observation.tool_name,
                    output=output,
                    error_type=observation.error_type,
                    error_message=(
                        self._capture(observation.error_message)
                        if self._policy.include_error_messages
                        and observation.error_message is not None
                        else None
                    ),
                    failure_origin=observation.failure_origin,
                )
            ]
            skill = self._successful_skill(pending, observation)
            if skill is not None:
                skill_name, source_path = skill
                facts.append(
                    _make_fact(
                        SkillFact,
                        common,
                        namespace=observation.namespace,
                        skill_id=_scope_id(
                            "skill",
                            observation.namespace,
                            observation.execution_id,
                        ),
                        execution_id=execution_id,
                        name=skill_name,
                        source_path=source_path,
                        agent_name=observation.agent_name,
                    )
                )
            return facts
        if isinstance(observation, ContextContributionObservation):
            if observation.failure_origin:
                self._failure_origin_seen = True
            parent_call_id = (
                None
                if observation.parent_call_id is None
                else self._callback_entries.get(observation.parent_call_id)
            )
            return [
                _make_fact(
                    ContextContributionFact,
                    common,
                    namespace=observation.namespace,
                    phase=observation.phase,
                    contribution_id=_scope_id(
                        "context",
                        observation.namespace,
                        observation.contribution_id,
                    ),
                    parent_call_id=parent_call_id,
                    context_kind=observation.context_kind,
                    name=observation.name,
                    input=(
                        None
                        if observation.input is None
                        else self._capture(observation.input)
                    ),
                    output=(
                        None
                        if observation.output is None
                        else self._capture(observation.output)
                    ),
                    error_type=observation.error_type,
                    failure_origin=observation.failure_origin,
                )
            ]
        if isinstance(observation, RunResumeCheckpointedObservation):
            return [
                _make_fact(
                    RunFact,
                    common,
                    phase="resume_checkpointed",
                    interrupt_ids=observation.native_interrupt_ids,
                )
            ]
        if isinstance(observation, RunObserverFailedObservation):
            return [
                _make_fact(
                    RunFact,
                    common,
                    phase="observer_failed",
                    observer_name=observation.observer_name,
                    error_type=observation.error_type,
                )
            ]
        if isinstance(observation, RunTerminalObservation):
            facts = self._terminal_settlement_facts(
                observation,
                common=common,
                source_id=source_id,
            )
            self._active_subagents.clear()
            self._active_runtime_tasks.clear()
            self._callback_entries.clear()
            self._tool_executions.clear()
            facts.append(
                _make_fact(
                    RunFact,
                    common,
                    phase="terminal",
                    outcome=observation.outcome,
                    code=observation.code,
                    error_type=observation.error_type,
                    failure_origin=(
                        observation.outcome == "failed"
                        and observation.error_type is not None
                        and not self._failure_origin_seen
                    ),
                )
            )
            return facts
        if isinstance(observation, RunClosedObservation):
            return [
                _make_fact(
                    RunFact,
                    common,
                    phase="closed",
                    outcome=observation.outcome,
                ),
            ]
        if isinstance(observation, NativeTaskObservation):
            self._remember_subagent_descriptors(observation)
        namespace = observation.namespace
        facts = self._subagent_start(observation, source_id=source_id)
        if isinstance(observation, NativeMessageObservation):
            facts.extend(self._message_facts(observation, source_id=source_id))
        elif isinstance(observation, NativeReasoningObservation):
            facts.extend(self._reasoning_facts(observation, source_id=source_id))
        elif isinstance(observation, NativeTaskObservation):
            task_key = (observation.namespace, observation.task_id)
            if observation.phase == "start":
                previous_task = self._active_runtime_tasks.get(task_key)
                if (
                    previous_task is not None
                    and previous_task.run_id == observation.identity.run_id
                ):
                    raise TraceCorruption("Native Runtime task started more than once")
                self._active_runtime_tasks[task_key] = _PendingRuntimeTask(
                    namespace=observation.namespace,
                    run_id=observation.identity.run_id,
                    source_task_id=observation.task_id,
                    task_name=observation.name,
                )
                task_phase = "started"
            else:
                pending_task = self._active_runtime_tasks.pop(task_key, None)
                if (
                    pending_task is not None
                    and pending_task.task_name != observation.name
                ):
                    raise TraceCorruption(
                        "Native Runtime task name changed before result"
                    )
                task_phase = (
                    "interrupted"
                    if observation.interrupts
                    else (
                        "failed" if observation.error_type is not None else "completed"
                    )
                )
            facts.append(
                _make_fact(
                    RuntimeTaskFact,
                    common,
                    namespace=namespace,
                    phase=task_phase,
                    task_id=_scope_id("task", namespace, observation.task_id),
                    source_task_id=observation.task_id,
                    task_name=observation.name,
                    triggers=observation.triggers,
                    input=(
                        None
                        if observation.input is None
                        else self._capture_structure(
                            _discard_private_state(
                                observation.input,
                                self._private_state_keys,
                            ),
                            divisor=2,
                        )
                    ),
                    result=(
                        None
                        if observation.result is None
                        else self._capture_structure(
                            _discard_private_state(
                                observation.result,
                                self._private_state_keys,
                            ),
                            divisor=2,
                        )
                    ),
                    error_type=observation.error_type,
                    interrupt_ids=tuple(
                        interrupt.id for interrupt in observation.interrupts
                    ),
                    failure_origin=(
                        task_phase == "failed"
                        and not self._context.call_tracking_enabled
                    ),
                )
            )
            if task_phase == "failed" and not self._context.call_tracking_enabled:
                self._failure_origin_seen = True
            facts.extend(self._subagent_completions(observation, source_id=source_id))
        elif isinstance(observation, NativeStateObservation):
            facts.extend(self._state_facts(observation, source_id=source_id))
        elif isinstance(observation, NativeExtraObservation):
            facts.append(
                _make_fact(
                    NativeExtraFact,
                    common,
                    namespace=namespace,
                    mode=observation.mode,
                    data_type=observation.data_type,
                    top_level_keys=tuple(
                        key
                        for key in observation.top_level_keys
                        if key not in self._private_state_keys
                    ),
                )
            )
        return facts

    def _terminal_settlement_facts(
        self,
        observation: RunTerminalObservation,
        *,
        common: Mapping[str, object],
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Settle Native work that emitted no result before the Run terminal.

        Native task results remain authoritative when present. Missing results cannot
        inherit a Run failure as their own error: interrupted work waits, explicit Run
        cancellation cancels it, and every other unmatched node is abandoned. Tool
        proposals remain waiting across an interrupt so a resumed checkpoint can still
        resolve the same proposal.
        """

        terminal_phase: Literal["interrupted", "cancelled", "abandoned"]
        if observation.outcome == "interrupted":
            terminal_phase = "interrupted"
        elif observation.outcome == "cancelled":
            terminal_phase = "cancelled"
        else:
            terminal_phase = "abandoned"
        facts: list[TraceSemanticFact] = []
        for key, task in sorted(self._active_runtime_tasks.items()):
            facts.append(
                _make_fact(
                    RuntimeTaskFact,
                    common,
                    namespace=task.namespace,
                    phase=terminal_phase,
                    task_id=_scope_id(
                        "task",
                        task.namespace,
                        task.source_task_id,
                    ),
                    source_task_id=task.source_task_id,
                    task_name=task.task_name,
                    interrupt_ids=(
                        observation.interrupt_ids
                        if terminal_phase == "interrupted"
                        else ()
                    ),
                )
            )
            self._active_runtime_tasks.pop(key, None)
        for namespace, (subagent_id, agent_name) in sorted(
            self._active_subagents.items()
        ):
            waiting = terminal_phase == "interrupted"
            subagent_status: Literal["waiting", "cancelled", "abandoned"]
            if waiting:
                subagent_status = "waiting"
            elif terminal_phase == "cancelled":
                subagent_status = "cancelled"
            else:
                subagent_status = "abandoned"
            facts.append(
                SubagentFact(
                    source_observation_id=source_id,
                    identity=observation.identity,
                    namespace=namespace,
                    occurred_at=observation.observed_at,
                    monotonic_ns=observation.monotonic_ns,
                    phase="updated" if waiting else "completed",
                    subagent_id=subagent_id,
                    agent_name=agent_name,
                    status=subagent_status,
                )
            )
            self._subagent_descriptors.pop(namespace, None)
        if terminal_phase != "interrupted":
            unresolved_tools = sorted(self._tool_started - self._tool_results)
            for namespace, tool_call_id in unresolved_tools:
                tool_name = self._tool_names.get((namespace, tool_call_id))
                if tool_name is None or not self._policy.traces_tool(tool_name):
                    continue
                facts.append(
                    _make_fact(
                        ToolFact,
                        common,
                        namespace=namespace,
                        phase=(
                            "cancelled"
                            if terminal_phase == "cancelled"
                            else "abandoned"
                        ),
                        tool_call_id=_scope_id("tool", namespace, tool_call_id),
                        source_tool_call_id=tool_call_id,
                        parent_call_id=self._tool_call_models.get(
                            (namespace, tool_call_id)
                        ),
                        tool_name=tool_name,
                    )
                )
                self._tool_results.add((namespace, tool_call_id))
                self._tool_completed.add((namespace, tool_call_id))
        return facts

    def _reasoning_facts(
        self,
        observation: NativeReasoningObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Reconcile one explicitly extracted reasoning delta or snapshot."""

        namespace = observation.namespace
        key = (namespace, observation.message_id)
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "namespace": namespace,
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        previous_extractor = self._reasoning_extractors.get(key)
        if (
            previous_extractor is not None
            and previous_extractor != observation.extractor
        ):
            raise TraceCorruption(
                "Reasoning extractor changed inside one scoped message"
            )
        self._reasoning_extractors[key] = observation.extractor
        facts: list[TraceSemanticFact] = []
        if not observation.snapshot:
            for active_key in tuple(self._active_reasoning):
                if active_key[0] == namespace and active_key != key:
                    facts.append(
                        self._complete_reasoning(
                            active_key,
                            common=common,
                        )
                    )
            captured = self._reasoning_policy.capture(
                observation.content,
                max_bytes=self._payload_budget,
            )
            if captured.disposition == "omitted" and key in self._active_reasoning:
                return facts
            if key in self._reasoning_completed and captured.disposition == "omitted":
                return facts
            self._reasoning_completed.discard(key)
            self._active_reasoning[key] = None
            self._track_reasoning_content(key, captured, append=True)
            facts.append(
                self._reasoning_content_fact(
                    observation,
                    common=common,
                    phase="content",
                    content=captured,
                )
            )
            return facts

        captured = self._reasoning_policy.capture(
            observation.content,
            max_bytes=self._payload_budget,
        )
        content_matches = (
            captured.disposition == "inline"
            and key in self._reasoning_contents
            and self._reasoning_contents[key] == captured.value
        )
        already_completed = key in self._reasoning_completed
        if already_completed and (content_matches or captured.disposition == "omitted"):
            self._active_reasoning.pop(key, None)
            return facts
        omission_already_recorded = (
            captured.disposition == "omitted" and key in self._active_reasoning
        )
        if not content_matches and not omission_already_recorded:
            self._track_reasoning_content(key, captured, append=False)
            facts.append(
                self._reasoning_content_fact(
                    observation,
                    common=common,
                    phase="reconciled",
                    content=captured,
                )
            )
        self._active_reasoning.pop(key, None)
        self._reasoning_completed.add(key)
        facts.append(self._complete_reasoning(key, common=common))
        return facts

    def _reasoning_content_fact(
        self,
        observation: NativeReasoningObservation,
        *,
        common: Mapping[str, object],
        phase: str,
        content: CapturedValue,
    ) -> TraceSemanticFact:
        return _make_fact(
            ReasoningFact,
            common,
            phase=phase,
            reasoning_id=_scope_id(
                "reasoning",
                observation.namespace,
                observation.message_id,
            ),
            message_id=_scope_id(
                "message",
                observation.namespace,
                observation.message_id,
            ),
            source_message_id=observation.message_id,
            extractor=observation.extractor,
            content=content,
        )

    def _complete_reasoning(
        self,
        key: tuple[tuple[str, ...], str],
        *,
        common: Mapping[str, object],
    ) -> TraceSemanticFact:
        namespace, source_message_id = key
        extractor = self._reasoning_extractors.get(key)
        if extractor is None:
            raise TraceCorruption("Reasoning completion has no registered extractor")
        self._active_reasoning.pop(key, None)
        self._reasoning_completed.add(key)
        return _make_fact(
            ReasoningFact,
            common,
            namespace=namespace,
            phase="completed",
            reasoning_id=_scope_id("reasoning", namespace, source_message_id),
            message_id=_scope_id("message", namespace, source_message_id),
            source_message_id=source_message_id,
            extractor=extractor,
        )

    def _complete_open_reasoning(
        self,
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        return [
            self._complete_reasoning(key, common=common)
            for key in tuple(self._active_reasoning)
        ]

    def _message_facts(
        self,
        observation: NativeMessageObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        message = observation.message
        if message.message_type == "tool" and message.tool_call_id is not None:
            tool_key = (observation.namespace, message.tool_call_id)
            tool_name = self._tool_names.get(tool_key) or message.name or "unknown"
            if not self._policy.traces_tool(tool_name):
                self._tool_result(
                    message,
                    namespace=observation.namespace,
                    common={
                        "source_observation_id": source_id,
                        "identity": observation.identity,
                        "namespace": observation.namespace,
                        "occurred_at": observation.observed_at,
                        "monotonic_ns": observation.monotonic_ns,
                    },
                )
                return []
        message_source_id = message.id or (
            f"anonymous-{self._message_fingerprint(message, observation.namespace)}"
        )
        message_id = _scope_id("message", observation.namespace, message_source_id)
        key = (observation.namespace, message_source_id)
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "namespace": observation.namespace,
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        facts: list[TraceSemanticFact] = []
        if message.message_type == "remove":
            if message.id is None:
                raise TraceCorruption("RemoveMessage requires a stable target ID")
            self._message_seen.discard(key)
            self._message_fingerprints.pop(key, None)
            self._message_contents.pop(key, None)
            self._message_completed.discard(key)
            return [
                _make_fact(
                    MessageFact,
                    common,
                    phase="removed",
                    message_id=message_id,
                    source_message_id=message.id,
                    role="other",
                )
            ]
        role = _message_role(message)
        if key not in self._message_seen:
            self._message_seen.add(key)
            facts.append(
                _make_fact(
                    MessageFact,
                    common,
                    phase="started",
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        if (
            message.message_type != "tool"
            and message.content != ""
            and message.content != []
        ):
            captured_content = self._message_content(message)
            self._track_message_content(
                key,
                captured_content,
                append=message.message_type == "assistant_chunk",
            )
            facts.append(
                _make_fact(
                    MessageFact,
                    common,
                    phase="content",
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    content=captured_content,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        facts.extend(
            self._tool_facts(
                message,
                namespace=observation.namespace,
                parent_message_id=message_id,
                common=common,
            )
        )
        if message.message_type == "tool" and message.tool_call_id is not None:
            facts.extend(
                self._tool_result(
                    message,
                    namespace=observation.namespace,
                    common=common,
                )
            )
        if message.message_type != "assistant_chunk":
            self._message_completed.add(key)
            facts.append(
                _make_fact(
                    MessageFact,
                    common,
                    phase="completed",
                    message_id=message_id,
                    source_message_id=message.id,
                    role=role,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
        return facts

    def _tool_facts(
        self,
        message: NativeMessageRecord,
        *,
        namespace: tuple[str, ...],
        parent_message_id: str,
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        facts: list[TraceSemanticFact] = []
        for chunk in message.tool_call_chunks:
            slot = (namespace, parent_message_id, chunk.index)
            if chunk.id is not None and chunk.name is not None:
                self._tool_slots[slot] = (chunk.id, chunk.name)
                key = (namespace, chunk.id)
                self._tool_names[key] = chunk.name
                if key not in self._tool_started:
                    self._tool_started.add(key)
                    if self._policy.traces_tool(chunk.name):
                        facts.append(
                            _make_fact(
                                ToolFact,
                                common,
                                phase="started",
                                tool_call_id=_scope_id("tool", namespace, chunk.id),
                                source_tool_call_id=chunk.id,
                                parent_call_id=self._tool_call_models.get(key),
                                tool_name=chunk.name,
                            )
                        )
            binding = self._tool_slots.get(slot)
            if binding is not None and chunk.arguments:
                tool_id, tool_name = binding
                tool_key = (namespace, tool_id)
                arguments = (
                    self._tool_argument_fragments.get(tool_key, "") + chunk.arguments
                )
                self._tool_argument_fragments[tool_key] = arguments
                parsed_arguments = _parsed_json(arguments)
                content = (
                    None
                    if parsed_arguments is None
                    else self._policy.capture_tool(
                        tool_name=tool_name,
                        value=parsed_arguments,
                        target="arguments",
                        max_bytes=self._payload_budget,
                    )
                )
                if content is not None:
                    self._tool_argument_snapshots.add(tool_key)
                if self._policy.traces_tool(tool_name) and content is not None:
                    facts.append(
                        _make_fact(
                            ToolFact,
                            common,
                            phase="arguments",
                            tool_call_id=_scope_id("tool", namespace, tool_id),
                            source_tool_call_id=tool_id,
                            parent_call_id=self._tool_call_models.get(tool_key),
                            tool_name=tool_name,
                            content=content,
                        )
                    )
        for call in message.tool_calls:
            key = (namespace, call.id)
            self._tool_names[key] = call.name
            if key not in self._tool_completed:
                if key not in self._tool_started:
                    self._tool_started.add(key)
                    if self._policy.traces_tool(call.name):
                        facts.append(
                            _make_fact(
                                ToolFact,
                                common,
                                phase="started",
                                tool_call_id=_scope_id("tool", namespace, call.id),
                                source_tool_call_id=call.id,
                                parent_call_id=self._tool_call_models.get(key),
                                tool_name=call.name,
                            ),
                        )
                streamed_arguments = self._tool_argument_fragments.get(key)
                arguments_match = False
                if streamed_arguments is not None:
                    try:
                        arguments_match = (
                            json.loads(streamed_arguments) == call.arguments
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        arguments_match = False
                if self._policy.traces_tool(call.name) and (
                    not arguments_match or key not in self._tool_argument_snapshots
                ):
                    facts.append(
                        _make_fact(
                            ToolFact,
                            common,
                            phase="arguments",
                            tool_call_id=_scope_id("tool", namespace, call.id),
                            source_tool_call_id=call.id,
                            parent_call_id=self._tool_call_models.get(key),
                            tool_name=call.name,
                            content=self._policy.capture_tool(
                                tool_name=call.name,
                                value=call.arguments,
                                target="arguments",
                                max_bytes=self._payload_budget,
                            ),
                        )
                    )
                if self._policy.traces_tool(call.name):
                    facts.append(
                        _make_fact(
                            ToolFact,
                            common,
                            phase="completed",
                            tool_call_id=_scope_id("tool", namespace, call.id),
                            source_tool_call_id=call.id,
                            parent_call_id=self._tool_call_models.get(key),
                            tool_name=call.name,
                        )
                    )
                self._tool_completed.add(key)
                self._tool_argument_fragments.pop(key, None)
        return facts

    def _tool_result(
        self,
        message: NativeMessageRecord,
        *,
        namespace: tuple[str, ...],
        common: Mapping[str, object],
    ) -> list[TraceSemanticFact]:
        assert message.tool_call_id is not None
        key = (namespace, message.tool_call_id)
        if key in self._tool_results:
            return []
        self._tool_results.add(key)
        self._tool_argument_fragments.pop(key, None)
        tool_name = self._tool_names.get(key) or message.name or "unknown"
        if not self._policy.traces_tool(tool_name):
            self._tool_completed.add(key)
            return []
        facts: list[TraceSemanticFact] = []
        if key not in self._tool_completed:
            facts.append(
                _make_fact(
                    ToolFact,
                    common,
                    phase="completed",
                    tool_call_id=_scope_id("tool", namespace, message.tool_call_id),
                    source_tool_call_id=message.tool_call_id,
                    parent_call_id=self._tool_call_models.get(key),
                    tool_name=tool_name,
                )
            )
            self._tool_completed.add(key)
        result_status = message.tool_status or "success"
        failure_origin = (
            result_status == "error" and key not in self._visible_tool_execution_calls
        )
        if failure_origin:
            self._failure_origin_seen = True
        facts.append(
            _make_fact(
                ToolFact,
                common,
                phase="result",
                tool_call_id=_scope_id("tool", namespace, message.tool_call_id),
                source_tool_call_id=message.tool_call_id,
                parent_call_id=self._tool_call_models.get(key),
                tool_name=tool_name,
                content=self._policy.capture_tool(
                    tool_name=tool_name,
                    value=message.content,
                    target="result",
                    max_bytes=self._payload_budget,
                ),
                result_status=result_status,
                failure_origin=failure_origin,
            )
        )
        return facts

    def _state_facts(
        self,
        observation: NativeStateObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        namespace = observation.namespace
        common = {
            "source_observation_id": source_id,
            "identity": observation.identity,
            "namespace": namespace,
            "occurred_at": observation.observed_at,
            "monotonic_ns": observation.monotonic_ns,
        }
        facts: list[TraceSemanticFact] = []
        public_state = cast(
            dict[str, JsonValue],
            self._policy.sanitize(
                _discard_private_state(
                    observation.state,
                    self._private_state_keys,
                )
            ),
        )
        previous = self._states.get(namespace, {})
        changes = {
            key: value
            for key, value in public_state.items()
            if key not in previous or previous[key] != value
        }
        removed = tuple(sorted(set(previous) - set(public_state)))
        plan_changed = "tinkerfin_plan" in changes
        state_changes = {
            key: value for key, value in changes.items() if key != "tinkerfin_plan"
        }
        if state_changes or removed:
            facts.append(
                _make_fact(
                    StateRevisionFact,
                    common,
                    revision_id=_scope_id(
                        "state",
                        namespace,
                        f"{self._observation_index}",
                    ),
                    changes=self._capture(state_changes),
                    removed_keys=removed,
                )
            )
        if plan_changed:
            plan_value = changes["tinkerfin_plan"]
            plan_revision, plan_status = _plan_metadata(plan_value)
            facts.append(
                _make_fact(
                    PlanRevisionFact,
                    common,
                    revision_id=_scope_id(
                        "plan",
                        namespace,
                        f"{self._observation_index}",
                    ),
                    revision=plan_revision,
                    status=plan_status,
                    plan=self._capture(plan_value),
                )
            )
        self._states[namespace] = dict(public_state)
        current_message_keys: set[tuple[tuple[str, ...], str]] = set()
        for message in observation.messages:
            message_source_id = message.id or (
                f"anonymous-{self._message_fingerprint(message, namespace)}"
            )
            key = (namespace, message_source_id)
            current_message_keys.add(key)
            fingerprint = self._message_fingerprint(message, namespace)
            if self._message_fingerprints.get(key) == fingerprint:
                continue
            if (
                key in self._message_seen
                and key not in self._message_fingerprints
                and message.message_type == "human"
            ):
                self._message_fingerprints[key] = fingerprint
                continue
            self._message_fingerprints[key] = fingerprint
            message_id = _scope_id("message", namespace, message_source_id)
            self._message_seen.add(key)
            captured_content = (
                None
                if message.message_type == "tool"
                else self._message_content(message)
            )
            content_matches = (
                captured_content is not None
                and captured_content.disposition == "inline"
                and key in self._message_contents
                and self._message_contents[key] == captured_content.value
            )
            phase = "completed" if content_matches else "reconciled"
            if captured_content is not None and not content_matches:
                self._track_message_content(
                    key,
                    captured_content,
                    append=False,
                )
            self._message_completed.add(key)
            facts.append(
                _make_fact(
                    MessageFact,
                    common,
                    phase=phase,
                    message_id=message_id,
                    source_message_id=message.id,
                    role=_message_role(message),
                    content=(None if content_matches else captured_content),
                    fingerprint=fingerprint,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
            )
            facts.extend(
                self._tool_facts(
                    message,
                    namespace=namespace,
                    parent_message_id=message_id,
                    common=common,
                )
            )
            if message.message_type == "tool" and message.tool_call_id is not None:
                facts.extend(
                    self._tool_result(message, namespace=namespace, common=common)
                )
        previous_message_keys = {
            key for key in self._message_fingerprints if key[0] == namespace
        }
        for key in previous_message_keys - current_message_keys:
            self._message_fingerprints.pop(key, None)
            self._message_seen.discard(key)
            self._message_contents.pop(key, None)
            self._message_completed.discard(key)
            facts.append(
                _make_fact(
                    MessageFact,
                    common,
                    phase="removed",
                    message_id=_scope_id("message", namespace, key[1]),
                    source_message_id=key[1],
                    role="other",
                )
            )
        current_interrupts = {(namespace, item.id) for item in observation.interrupts}
        for interrupt in observation.interrupts:
            key = (namespace, interrupt.id)
            if key in self._pending_interactions:
                continue
            # LangGraph v2 propagates a dynamic child's interrupt through every
            # ancestor values snapshot. The deepest observed scope owns correlation;
            # treating the later root copy as another review would compare child
            # actions with the parent's unrelated Tool calls and fail the whole Run.
            if any(
                pending_id == interrupt.id
                and len(pending_namespace) > len(namespace)
                and pending_namespace[: len(namespace)] == namespace
                for pending_namespace, pending_id in self._pending_interactions
            ):
                continue
            interaction_kind = _interaction_kind(interrupt.value)
            tool_call_ids = _interaction_tool_call_ids(
                interrupt.value,
                observation.messages,
            )
            self._pending_interactions[key] = _PendingInteraction(
                kind=interaction_kind,
                tool_call_ids=tool_call_ids,
            )
            facts.append(
                _make_fact(
                    InteractionFact,
                    common,
                    phase="opened",
                    interaction_id=_scope_id("interaction", namespace, interrupt.id),
                    source_interaction_id=interrupt.id,
                    interaction_kind=interaction_kind,
                    tool_call_ids=tool_call_ids,
                    status="pending",
                    payload=self._capture_interaction(interrupt.value),
                )
            )
        for key in tuple(self._pending_interactions):
            if key[0] != namespace or key in current_interrupts:
                continue
            pending = self._pending_interactions.pop(key)
            facts.append(
                _make_fact(
                    InteractionFact,
                    common,
                    phase="resolved",
                    interaction_id=_scope_id("interaction", namespace, key[1]),
                    source_interaction_id=key[1],
                    interaction_kind=pending.kind,
                    tool_call_ids=pending.tool_call_ids,
                    status="resolved",
                )
            )
        return facts

    def _subagent_start(
        self,
        observation: RuntimeObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        if not isinstance(
            observation,
            (
                NativeMessageObservation,
                NativeReasoningObservation,
                NativeTaskObservation,
                NativeStateObservation,
                NativeExtraObservation,
            ),
        ):
            return []
        namespace = observation.namespace
        if not namespace:
            return []
        if namespace in self._active_subagents:
            return []
        descriptor = self._subagent_descriptors.get(namespace)
        agent_name: str | None = None if descriptor is None else descriptor.agent_name
        if isinstance(observation, NativeMessageObservation):
            raw_name = observation.metadata.get("lc_agent_name")
            if isinstance(raw_name, str) and raw_name:
                if agent_name is not None and raw_name != agent_name:
                    raise TraceCorruption(
                        "Subagent name conflicts with its parent task Tool"
                    )
                agent_name = raw_name
        subagent_id = _scope_id("subagent", namespace, namespace[-1])
        self._active_subagents[namespace] = (subagent_id, agent_name)
        return [
            SubagentFact(
                source_observation_id=source_id,
                identity=observation.identity,
                namespace=namespace,
                occurred_at=observation.observed_at,
                monotonic_ns=observation.monotonic_ns,
                phase="started",
                subagent_id=subagent_id,
                agent_name=agent_name,
                parent_tool_call_id=(
                    None if descriptor is None else descriptor.parent_tool_call_id
                ),
                parent_execution_id=(
                    None
                    if descriptor is None
                    else self._tool_execution_by_call.get(
                        (namespace[:-1], descriptor.parent_tool_call_id)
                    )
                ),
                input=(
                    None
                    if descriptor is None
                    else self._policy.capture_tool(
                        tool_name="task",
                        value={
                            "description": descriptor.description,
                            "subagent_type": descriptor.agent_name,
                        },
                        target="arguments",
                        max_bytes=self._payload_budget,
                    )
                ),
                status="running",
            )
        ]

    def _remember_subagent_descriptors(
        self,
        observation: NativeTaskObservation,
    ) -> None:
        """Index verified Deep Agents task Tool inputs before child parts arrive.

        Deep Agents 0.7.5 emits the parent ``tools`` task start before each child
        namespace. The task input carries model Tool calls; only exact ``task`` calls
        with the locked ``description`` and ``subagent_type`` fields establish public
        subagent identity. Contract coverage mirrors the adapter provenance tests.
        """

        if (
            observation.phase != "start"
            or observation.name != "tools"
            or not isinstance(observation.input, list)
        ):
            return
        descriptors: list[tuple[str, str, str]] = []
        for raw_call in observation.input:
            if not isinstance(raw_call, dict) or raw_call.get("name") != "task":
                continue
            raw_id = raw_call.get("id")
            raw_args = raw_call.get("args")
            if (
                not isinstance(raw_id, str)
                or not raw_id
                or not isinstance(raw_args, dict)
            ):
                continue
            description = raw_args.get("description")
            agent_name = raw_args.get("subagent_type")
            if (
                not isinstance(description, str)
                or not description
                or not isinstance(agent_name, str)
                or not agent_name
            ):
                continue
            descriptors.append((raw_id, agent_name, description))
        multiple = len(descriptors) > 1
        for index, (tool_call_id, agent_name, description) in enumerate(descriptors):
            suffix = (
                f"{observation.task_id}:{index}" if multiple else observation.task_id
            )
            namespace = (*observation.namespace, f"tools:{suffix}")
            descriptor = _SubagentDescriptor(
                parent_tool_call_id=tool_call_id,
                parent_task_id=observation.task_id,
                agent_name=agent_name,
                description=description,
            )
            existing = self._subagent_descriptors.get(namespace)
            if existing is not None and existing != descriptor:
                raise TraceCorruption(
                    "Subagent parent task Tool changed before child execution"
                )
            self._subagent_descriptors[namespace] = descriptor

    def _subagent_completions(
        self,
        observation: NativeTaskObservation,
        *,
        source_id: str,
    ) -> list[TraceSemanticFact]:
        """Close direct child graph scopes when their owning task returns."""

        if observation.phase != "result" or observation.interrupts:
            return []
        parent = observation.namespace
        # LangGraph v2 scopes encode a direct child as ``<node>:<owning-task-id>``.
        # The locked Plan fixture and the parent-task regression test verify this join.
        matches = [
            namespace
            for namespace in self._active_subagents
            if len(namespace) == len(parent) + 1
            and namespace[: len(parent)] == parent
            and (
                (
                    self._subagent_descriptors[namespace].parent_task_id
                    if namespace in self._subagent_descriptors
                    else namespace[-1].partition(":")[2]
                )
                == observation.task_id
            )
        ]
        facts: list[TraceSemanticFact] = []
        for namespace in sorted(matches):
            subagent_id, agent_name = self._active_subagents.pop(namespace)
            self._subagent_descriptors.pop(namespace, None)
            facts.append(
                SubagentFact(
                    source_observation_id=source_id,
                    identity=observation.identity,
                    namespace=namespace,
                    occurred_at=observation.observed_at,
                    monotonic_ns=observation.monotonic_ns,
                    phase="completed",
                    subagent_id=subagent_id,
                    agent_name=agent_name,
                    status="failed" if observation.error_type else "succeeded",
                )
            )
        return facts

    def _resolve_pending_interaction(
        self,
        interrupt_id: str,
    ) -> tuple[tuple[str, ...], _PendingInteraction]:
        matches = [key for key in self._pending_interactions if key[1] == interrupt_id]
        if len(matches) > 1:
            raise TraceCorruption(
                "Native interrupt ID is ambiguous across graph scopes",
                context={"interrupt_id": interrupt_id},
            )
        if not matches:
            return (), _PendingInteraction(kind="resume", tool_call_ids=())
        key = matches[0]
        pending = self._pending_interactions.pop(key)
        return key[0], pending

    def _successful_skill(
        self,
        pending: _PendingToolExecution,
        observation: ToolExecutionObservation,
    ) -> tuple[str, str] | None:
        """Recognize only a successful exact configured ``SKILL.md`` read."""

        if observation.phase != "completed" or pending.tool_name != "read_file":
            return None
        if not isinstance(pending.input, dict):
            return None
        raw_path = pending.input.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        candidate = PurePosixPath(raw_path)
        if candidate.name != "SKILL.md" or not candidate.parent.name:
            return None
        for source in self._context.skill_sources:
            if source.agent_name != pending.agent_name:
                continue
            if candidate.parent.parent == PurePosixPath(source.path):
                return candidate.parent.name, str(candidate)
        return None

    @property
    def _payload_budget(self) -> int:
        return max(1024, self._limits.max_event_bytes // 2)

    def _capture(self, value: JsonValue, *, divisor: int = 1) -> CapturedValue:
        return self._policy.capture(
            value,
            max_bytes=max(1024, self._payload_budget // divisor),
        )

    def _capture_structure(
        self,
        value: JsonValue,
        *,
        divisor: int = 1,
    ) -> CapturedValue:
        return self._policy.capture_structure(
            value,
            max_bytes=max(1024, self._payload_budget // divisor),
        )

    def _track_message_content(
        self,
        key: tuple[tuple[str, ...], str],
        content: CapturedValue,
        *,
        append: bool,
    ) -> None:
        if content.disposition == "omitted":
            self._message_contents.pop(key, None)
            return
        if not append or key not in self._message_contents:
            self._message_contents[key] = content.value
            return
        combined = _append_json_content(self._message_contents[key], content.value)
        bounded = self._policy.capture(combined, max_bytes=self._payload_budget)
        if bounded.disposition == "inline":
            self._message_contents[key] = bounded.value
        else:
            self._message_contents.pop(key, None)

    def _track_reasoning_content(
        self,
        key: tuple[tuple[str, ...], str],
        content: CapturedValue,
        *,
        append: bool,
    ) -> None:
        if content.disposition == "omitted":
            self._reasoning_contents.pop(key, None)
            return
        if not append or key not in self._reasoning_contents:
            self._reasoning_contents[key] = content.value
            return
        combined = _append_json_content(self._reasoning_contents[key], content.value)
        bounded = self._reasoning_policy.capture(
            combined,
            max_bytes=self._payload_budget,
        )
        if bounded.disposition == "inline":
            self._reasoning_contents[key] = bounded.value
        else:
            self._reasoning_contents.pop(key, None)

    def _capture_interaction(self, value: JsonValue) -> CapturedValue:
        if not isinstance(value, dict):
            return self._capture_required_interaction(value)
        raw_actions = value.get("action_requests")
        if not isinstance(raw_actions, list):
            return self._capture_required_interaction(value)
        actions: list[JsonValue] = []
        for raw_action in raw_actions:
            if not isinstance(raw_action, dict):
                actions.append(
                    self._capture_structure(raw_action).model_dump(
                        mode="json",
                        by_alias=True,
                    )
                )
                continue
            raw_name = raw_action.get("name")
            tool_name = (
                raw_name if isinstance(raw_name, str) and raw_name else "unknown"
            )
            raw_arguments = raw_action.get("args")
            arguments: JsonValue = raw_arguments if raw_arguments is not None else {}
            action: dict[str, JsonValue] = {"name": tool_name}
            captured_arguments = self._policy.capture_tool(
                tool_name=tool_name,
                value=arguments,
                target="arguments",
                max_bytes=self._payload_budget,
            )
            action["arguments"] = captured_arguments.model_dump(
                mode="json",
                by_alias=True,
            )
            description = raw_action.get("description")
            if (
                isinstance(description, str)
                and description
                and captured_arguments.disposition == "inline"
                and self._policy.captures_review_description(tool_name)
            ):
                action["description"] = description
            actions.append(action)
        safe_value = {
            key: item for key, item in value.items() if key != "action_requests"
        }
        safe_value["action_requests"] = actions
        return self._capture_required_interaction(safe_value)

    def _capture_required_interaction(self, value: JsonValue) -> CapturedValue:
        """Reject a pause that cannot be reconstructed from its durable Trace."""

        captured = self._capture(value)
        if captured.disposition == "omitted":
            raise TraceCaptureRejected(
                "Pending interaction payload exceeds the safe Trace boundary"
            )
        return captured

    def _message_fingerprint(
        self,
        message: NativeMessageRecord,
        namespace: tuple[str, ...],
    ) -> str:
        if message.message_type == "tool":
            tool_name = (
                self._tool_names.get((namespace, message.tool_call_id or ""))
                or message.name
                or "unknown"
            )
            content = self._policy.capture_tool(
                tool_name=tool_name,
                value=message.content,
                target="result",
                max_bytes=self._payload_budget,
            )
        else:
            content = self._message_content(message)
        safe_calls: list[JsonValue] = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": self._policy.capture_tool(
                    tool_name=call.name,
                    value=call.arguments,
                    target="arguments",
                    max_bytes=self._payload_budget,
                ).model_dump(mode="json", by_alias=True),
            }
            for call in message.tool_calls
        ]
        safe_chunks: list[JsonValue] = [
            {
                "index": chunk.index,
                "id": chunk.id,
                "name": chunk.name,
                "safeSizeBytes": len(chunk.arguments.encode()),
            }
            for chunk in message.tool_call_chunks
        ]
        return _fingerprint(
            {
                "messageType": message.message_type,
                "id": message.id,
                "name": message.name,
                "content": content.model_dump(mode="json", by_alias=True),
                "toolCalls": safe_calls,
                "toolCallChunks": safe_chunks,
                "toolCallId": message.tool_call_id,
                "toolStatus": message.tool_status,
            }
        )

    def _message_content(self, message: NativeMessageRecord) -> CapturedValue:
        return self._capture(message.content)


class Tracer:
    """Record Runtime observations and expose deterministic Trace queries.

    Provider reasoning requires an independently configured Runtime extractor. This
    observer omits extracted content unless ``reasoning_capture_policy`` explicitly
    authorizes retention.
    """

    def __init__(
        self,
        *,
        store: TraceStore | None = None,
        capture_policy: CapturePolicy | None = None,
        reasoning_capture_policy: ReasoningCapturePolicy | None = None,
        limits: TraceLimits | None = None,
        write_policy: TraceWritePolicy | None = None,
        projections: tuple[TraceProjection[Any, Any], ...] = (),
    ) -> None:
        """Configure borrowed persistence, capture, batching, and query Projections.

        Args:
            store: Borrowed Store. Omitting it creates a bounded in-process Store owned
                only by this Tracer value.
            capture_policy: Public semantic capture and redaction policy. Omitting it
                retains complete sanitized Tool content through ``public_history``.
            reasoning_capture_policy: Independent authorization for extracted provider
                reasoning content.
            limits: Capacity contract; when supplied it must equal ``store.limits``.
            write_policy: Bounded asynchronous batching and backpressure policy.
            projections: Pure business Projections evaluated and cached only on query.

        Raises:
            TypeError: A supplied integration has the wrong public type.
            ValueError: Store limits or Projection registrations conflict.
        """

        if store is not None and not isinstance(store, TraceStore):
            raise TypeError("store must implement TraceStore")
        if limits is not None and not isinstance(limits, TraceLimits):
            raise TypeError("limits must be a TraceLimits or None")
        if capture_policy is not None and not isinstance(
            capture_policy,
            CapturePolicy,
        ):
            raise TypeError("capture_policy must be a CapturePolicy or None")
        if reasoning_capture_policy is not None and not isinstance(
            reasoning_capture_policy,
            ReasoningCapturePolicy,
        ):
            raise TypeError(
                "reasoning_capture_policy must be a ReasoningCapturePolicy or None"
            )
        if write_policy is not None and not isinstance(write_policy, TraceWritePolicy):
            raise TypeError("write_policy must be a TraceWritePolicy or None")
        resolved_limits = (
            limits
            if limits is not None
            else (store.limits if store is not None else TraceLimits())
        )
        resolved_store = (
            InMemoryTraceStore(limits=resolved_limits) if store is None else store
        )
        if limits is not None and resolved_store.limits != limits:
            raise ValueError("Tracer limits must match Store limits")
        self._store = resolved_store
        self._capture_policy = (
            CapturePolicy.public_history() if capture_policy is None else capture_policy
        )
        self._reasoning_capture_policy = (
            ReasoningCapturePolicy.omitted()
            if reasoning_capture_policy is None
            else reasoning_capture_policy
        )
        self._limits = resolved_limits
        self._write_policy = write_policy or TraceWritePolicy()
        self._projections = _projection_registry(projections)
        self._sessions: dict[str, set[_TracingSession]] = {}

    @property
    def store(self) -> TraceStore:
        """Return the borrowed Store used for writes and queries."""

        return self._store

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        """Open one fail-closed request-scoped semantic observation session.

        The Store remains borrowed by the Tracer. The returned session owns the exact
        Run writer, restores only the selected parent lineage for dedupe, and closes the
        writer on every partial-start failure before propagating the primary error.

        Args:
            context: Canonical Runtime input, Profile, lineage, mode, and privacy facts.

        Returns:
            A request-scoped Observer session owned by the Runtime until close.

        Raises:
            TypeError: ``context`` has the wrong public type.
            TraceStoreError: Writer creation or bounded lineage reads fail.
            TraceStoreProtocolError: The Store returns an invalid writer or snapshot.
            TraceCorruption: Existing lineage facts violate semantic invariants.
        """

        if not isinstance(context, RunSourceContext):
            raise TypeError("context must be a RunSourceContext")
        writer = await self._store.open_writer(context.identity)
        if not isinstance(writer, TraceWriter):
            raise TraceStoreProtocolError("Trace Store returned an invalid writer")
        try:
            snapshot = await self._store.snapshot_key(writer.key)
            if not isinstance(snapshot, StoreThreadSnapshot):
                raise TraceStoreProtocolError(
                    "Trace Store returned an invalid thread snapshot"
                )
            core_state = await load_core_projection_state(
                self._store,
                writer.key,
                as_of_seq=snapshot.as_of_seq,
            )
            prior_run_ids = select_prior_run_ids(
                core_state,
                parent_run_id=context.parent_run_id,
            )
            prior_events = await read_lineage_events(
                self._store,
                writer.key,
                run_ids=prior_run_ids,
                as_of_seq=snapshot.as_of_seq,
            )
            session = _TracingSession(
                writer=writer,
                store=self._store,
                context=context,
                capture_policy=self._capture_policy,
                reasoning_capture_policy=self._reasoning_capture_policy,
                limits=self._limits,
                write_policy=self._write_policy,
                on_closed=self._session_closed,
                prior_events=prior_events,
            )
            self._sessions.setdefault(context.identity.thread_id, set()).add(session)
            return session
        except BaseException as error:
            try:
                await writer.aclose()
            except BaseException as close_error:  # noqa: BLE001 - preserve primary error
                error.add_note(
                    "Trace writer cleanup failed: "
                    f"{type(close_error).__module__}.{type(close_error).__qualname__}"
                )
            raise

    async def get(
        self,
        thread_id: str,
        *,
        head_run_id: str | None = None,
        limit: int = 100,
        history_cursor: str | None = None,
        projections: tuple[str, ...] = (),
    ) -> TraceThread:
        """Return one fixed-as-of semantic handle for the current generation.

        Active sessions owned by this Tracer are forced before the Store snapshot. The
        opaque history cursor can only expand the same generation, head, and as-of
        prefix; it never admits facts committed after that boundary.

        Args:
            thread_id: Canonical Trace thread identity.
            head_run_id: Optional explicit branch head.
            limit: Positive number of latest Turns in the initial window.
            history_cursor: Optional opaque cursor from the same fixed prefix.
            projections: Unique registered Projection names requested for this query.

        Returns:
            Immutable Trace view with bounded history and a closeable follow iterator.

        Raises:
            ValueError: Identity, limit, cursor, or Projection names are invalid.
            TraceThreadNotFound: The thread or cursor generation is unavailable.
            TraceRunNotFound: The explicitly selected Run has not entered the generation.
            AmbiguousTraceHead: Multiple heads exist without an explicit selection.
            TraceStoreError: Snapshot, event, or Projection checkpoint access fails.
            TraceProjectionFailed: A requested business Projection cannot be evaluated.
        """

        if (
            not isinstance(thread_id, str)
            or not thread_id
            or thread_id != thread_id.strip()
        ):
            raise ValueError("thread_id must be canonical non-empty text")
        if head_run_id is not None and (
            not isinstance(head_run_id, str)
            or not head_run_id
            or head_run_id != head_run_id.strip()
        ):
            raise ValueError("head_run_id must be canonical non-empty text or None")
        if any(
            not isinstance(name, str) or not name or name != name.strip()
            for name in projections
        ):
            raise ValueError("Projection names must be canonical non-empty text")
        if len(set(projections)) != len(projections):
            raise ValueError("Trace Projection names must be unique")
        if history_cursor is not None and (
            not isinstance(history_cursor, str) or not history_cursor
        ):
            raise ValueError("history_cursor must be non-empty text or None")
        sessions = tuple(self._sessions.get(thread_id, ()))
        if sessions:
            await asyncio.gather(*(session.flush() for session in sessions))
        snapshot, resolved_head, resolved_limit = await resolve_history_request(
            self._store,
            thread_id=thread_id,
            head_run_id=head_run_id,
            history_cursor=history_cursor,
            limit=limit,
        )
        if not isinstance(snapshot, StoreThreadSnapshot):
            raise TraceStoreProtocolError(
                "Trace Store returned an invalid thread snapshot"
            )
        return await build_trace_thread(
            store=self._store,
            snapshot=snapshot,
            head_run_id=resolved_head,
            limit=resolved_limit,
            projections=self._projections,
            projection_names=projections,
        )

    async def query(
        self,
        thread_id: str,
        *,
        where: TraceFilter | None = None,
        head_run_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TraceQuery:
        """Return one directly filtered current entry page with live following.

        Args:
            thread_id: Canonical Trace thread identity.
            where: Functional indexed filters. Omitting it selects every entry kind.
            head_run_id: Optional explicit branch head.
            cursor: Optional opaque cursor from the same generation and filter.
            limit: Positive maximum matching entries before requested ancestors.

        Returns:
            Current page values and a closeable filtered follow iterator.

        Raises:
            ValueError: An identity, filter, or limit is invalid.
            InvalidTraceCursor: The cursor belongs to another query.
            TraceThreadNotFound: The current generation is unavailable.
            TraceStoreProtocolError: The Store does not support direct entry queries.
        """

        if (
            not isinstance(thread_id, str)
            or not thread_id
            or thread_id != thread_id.strip()
        ):
            raise ValueError("thread_id must be canonical non-empty text")
        if head_run_id is not None and (
            not isinstance(head_run_id, str)
            or not head_run_id
            or head_run_id != head_run_id.strip()
        ):
            raise ValueError("head_run_id must be canonical non-empty text or None")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be an integer between 1 and 1000")
        if where is not None and not isinstance(where, TraceFilter):
            raise TypeError("where must be a TraceFilter or None")
        resolved_filter = where or TraceFilter()
        page, key = await self._query_entry_page(
            thread_id,
            where=resolved_filter,
            head_run_id=head_run_id,
            cursor=cursor,
            limit=limit,
            expected_key=None,
        )

        async def refresh() -> TraceEntryPage:
            refreshed, _key = await self._query_entry_page(
                thread_id,
                where=resolved_filter,
                head_run_id=head_run_id,
                cursor=cursor,
                limit=limit,
                expected_key=key,
            )
            return refreshed

        return TraceQuery(page, store=self._store, key=key, refresh=refresh)

    async def rebuild_entries(self, thread_id: str) -> int:
        """Reconstruct current query entries without changing authoritative facts.

        Active sessions owned by this Tracer are flushed first. A concurrent commit by
        another process makes the rebuild fail instead of publishing a partial index.

        Args:
            thread_id: Canonical Trace thread identity.

        Returns:
            Number of rebuilt query entries in the current generation.

        Raises:
            ValueError: ``thread_id`` is not canonical non-empty text.
            TraceThreadNotFound: The current generation is unavailable.
            TraceStoreProtocolError: The Store cannot rebuild entries or the Ledger
                changes during reconstruction.
        """

        if (
            not isinstance(thread_id, str)
            or not thread_id
            or thread_id != thread_id.strip()
        ):
            raise ValueError("thread_id must be canonical non-empty text")
        sessions = tuple(self._sessions.get(thread_id, ()))
        if sessions:
            await asyncio.gather(*(session.flush() for session in sessions))
        store = self._store
        if not isinstance(store, TraceEntryRebuildStore):
            raise TraceStoreProtocolError(
                "Trace Store does not rebuild indexed entries"
            )
        snapshot = await store.snapshot(thread_id)
        return await store.rebuild_trace_entries(snapshot.key)

    async def _query_entry_page(
        self,
        thread_id: str,
        *,
        where: TraceFilter,
        head_run_id: str | None,
        cursor: str | None,
        limit: int,
        expected_key: TraceThreadKey | None,
    ) -> tuple[TraceEntryPage, TraceThreadKey]:
        sessions = tuple(self._sessions.get(thread_id, ()))
        if sessions:
            await asyncio.gather(*(session.flush() for session in sessions))
        snapshot = await self._store.snapshot(thread_id)
        if expected_key is not None and snapshot.key != expected_key:
            raise TraceStoreProtocolError(
                "Trace entry follow generation changed during the query"
            )
        core_state = await load_core_projection_state(
            self._store,
            snapshot.key,
            as_of_seq=snapshot.as_of_seq,
        )
        window = select_core_projection_window(
            core_state,
            head_run_id=head_run_id,
            turn_limit=max(1, len(core_state.turn_order)),
        )
        core = project_core_checkpoint(
            core_state,
            head_run_id=head_run_id,
            turn_limit=1,
            active_run_ids=snapshot.active_run_ids,
        )
        before_started_at: datetime | None = None
        before_entry_id: str | None = None
        if cursor is not None:
            before_started_at, before_entry_id = decode_entry_cursor(
                cursor,
                key=snapshot.key,
                head_run_id=head_run_id,
                where=where,
            )
        store = self._store
        if not isinstance(store, TraceEntryStore):
            raise TraceStoreProtocolError(
                "Trace Store does not provide indexed entry queries"
            )
        records = await store.query_trace_entries(
            snapshot.key,
            run_ids=tuple(sorted(core.selected_run_ids)),
            where=where,
            limit=limit,
            before_started_at=before_started_at,
            before_entry_id=before_entry_id,
        )
        projected_items: list[TraceEntry] = []
        selected_turn_ids: set[str] = set()
        for record in records.entries:
            turn_id = window.run_turns.get(record.run_id)
            if turn_id is None:
                raise TraceStoreProtocolError(
                    "Trace entry Run has no selected Turn ownership"
                )
            selected_turn_ids.add(turn_id)
            projected_items.append(project_trace_entry(record, turn_id=turn_id))
        tracked_run_ids = {
            fact.identity.run_id
            for fact in core_state.runs[window.selected_head].tree_facts
            if isinstance(fact, AgentStepFact)
            and fact.phase == "started"
            and fact.step_kind == "agent"
        }
        next_cursor = (
            encode_entry_cursor(
                key=snapshot.key,
                head_run_id=head_run_id,
                where=where,
                before_started_at=records.next_started_at,
                before_entry_id=records.next_entry_id,
            )
            if records.has_more
            and records.next_started_at is not None
            and records.next_entry_id is not None
            else None
        )
        return (
            TraceEntryPage(
                turns=_entry_turns(
                    core_state,
                    window,
                    selected_turn_ids=selected_turn_ids,
                ),
                items=tuple(projected_items),
                next_cursor=next_cursor,
                as_of_seq=records.as_of_seq,
                facets=records.facets,
                completeness=TraceEntryCompleteness(
                    call_tracking_missing=not records.call_tracking_present,
                    execution_tree_missing=not window.selected_run_ids
                    <= tracked_run_ids,
                ),
            ),
            snapshot.key,
        )

    def _session_closed(self, session: _TracingSession) -> None:
        """Remove one settled session from same-Tracer read-your-writes flushing."""

        thread_id = session._context.identity.thread_id
        sessions = self._sessions.get(thread_id)
        if sessions is None:
            return
        sessions.discard(session)
        if not sessions:
            self._sessions.pop(thread_id, None)


def _entry_turns(
    state: CoreProjectionState,
    window: CoreProjectionWindow,
    *,
    selected_turn_ids: set[str],
) -> tuple[TraceTurn, ...]:
    """Resolve only the selected entry page's Turns from the core checkpoint."""

    lineage_turn_ids = {
        window.run_turns[run_id]
        for run_id in window.selected_run_ids
        if run_id in window.run_turns
    }
    ordered_turn_ids = [
        turn_id for turn_id in state.turn_order if turn_id in lineage_turn_ids
    ]
    ordinals = {
        turn_id: ordinal for ordinal, turn_id in enumerate(ordered_turn_ids, start=1)
    }
    turns: list[TraceTurn] = []
    for turn_id in ordered_turn_ids:
        if turn_id not in selected_turn_ids:
            continue
        checkpoint = state.turns.get(turn_id)
        if checkpoint is None or not checkpoint.run_ids:
            raise TraceStoreProtocolError("Trace entry Turn checkpoint is unavailable")
        run_checkpoints = tuple(
            state.runs[run_id] for run_id in checkpoint.run_ids if run_id in state.runs
        )
        if not run_checkpoints:
            raise TraceStoreProtocolError("Trace entry Turn has no available Run")
        user_message = None
        if checkpoint.user_message_id is not None:
            candidates = (
                message
                for run in run_checkpoints
                for message in run.messages
                if message.role == "user"
                and message.source_id == checkpoint.user_message_id
            )
            user_message = min(
                candidates, key=lambda item: item.trace_seq, default=None
            )
        turns.append(
            TraceTurn(
                id=turn_id,
                ordinal=ordinals[turn_id],
                started_at=min(run.started_at for run in run_checkpoints),
                user_message=(
                    None if user_message is None else user_message.model_copy(deep=True)
                ),
            )
        )
    return tuple(turns)


def _projection_registry(
    projections: Sequence[RegisteredTraceProjection],
) -> Mapping[str, RegisteredTraceProjection]:
    values: dict[str, RegisteredTraceProjection] = {}
    for projection in projections:
        if not isinstance(projection, TraceProjection):
            raise TypeError("projection must implement TraceProjection")
        name = projection.name
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("Projection name must be canonical non-empty text")
        if name in values:
            raise ValueError(f"duplicate Trace Projection name: {name}")
        if not issubclass(projection.state_type, BaseModel) or not issubclass(
            projection.result_type, BaseModel
        ):
            raise TypeError("Projection state and result types must be Pydantic models")
        values[name] = projection
    return MappingProxyType(values)


def _user_message(value: JsonValue) -> tuple[str, JsonValue] | None:
    """Return the final user message from the authoritative top-level channel."""

    if not isinstance(value, dict):
        return None
    raw_messages = value.get("messages")
    if isinstance(raw_messages, dict) and raw_messages.get("$type") == "tuple":
        raw_messages = raw_messages.get("items")
    if not isinstance(raw_messages, list):
        return None
    candidates: list[tuple[str, JsonValue]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        if item.get("$type") == "langchain.message":
            wrapped = item.get("value")
            if isinstance(wrapped, dict) and wrapped.get("type") in {"human", "user"}:
                data = wrapped.get("data")
                message_id = data.get("id") if isinstance(data, dict) else None
                content = data.get("content") if isinstance(data, dict) else None
                if isinstance(message_id, str) and message_id and content is not None:
                    candidates.append((message_id, cast(JsonValue, content)))
            continue
        if item.get("role") == "user":
            message_id = item.get("id")
            content = item.get("content")
            if isinstance(message_id, str) and message_id and content is not None:
                candidates.append((message_id, content))
    return candidates[-1] if candidates else None


def _parsed_json(value: str) -> JsonValue | None:
    try:
        return cast(
            JsonValue,
            json.loads(
                value,
                parse_constant=lambda _value: _raise_invalid_json_constant(),
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _append_json_content(current: JsonValue, delta: JsonValue) -> JsonValue:
    if isinstance(current, str) and isinstance(delta, str):
        return current + delta
    if isinstance(current, list) and isinstance(delta, list):
        return [*current, *delta]
    return delta


def _raise_invalid_json_constant() -> None:
    raise ValueError("non-finite JSON constants are not supported")


def _discard_private_state(
    value: JsonValue,
    private_keys: frozenset[str],
) -> JsonValue:
    """Remove only Runtime-declared top-level state channels."""

    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in private_keys}


def _plan_metadata(value: JsonValue) -> tuple[int | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    raw_revision = value.get("revision")
    revision = (
        raw_revision
        if isinstance(raw_revision, int)
        and not isinstance(raw_revision, bool)
        and raw_revision >= 0
        else None
    )
    raw_status = value.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else None
    return revision, status


def _interaction_kind(value: JsonValue) -> str:
    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str) and kind:
            return kind
        if "action_requests" in value:
            return "tool_approval"
    return "input_required"


def _interaction_tool_call_ids(
    value: JsonValue,
    messages: tuple[NativeMessageRecord, ...],
) -> tuple[str, ...]:
    """Correlate reviewed actions to one unique ordered Tool-call subsequence.

    Deep Agents publishes only policy-selected actions in an interrupt, while the
    authoritative message can also contain unreviewed Tool calls. Correlation therefore
    matches exact ``name + args`` in model order, never arrival order or Tool name alone.
    The dynamic-programming count is capped at two because the only meaningful outcomes
    are missing, unique, and ambiguous.

    Args:
        value: Native interrupt value from the root or subgraph state snapshot.
        messages: Complete Native message records from the same state snapshot.

    Returns:
        Raw Tool call IDs in the same position order as ``action_requests``. Non-Tool
        runtime interrupts return an empty tuple.

    Raises:
        TraceCorruption: A Tool review is malformed, too large to correlate safely,
            missing its checkpoint Tool calls, or ambiguous.
    """

    if not isinstance(value, dict) or "action_requests" not in value:
        return ()
    raw_actions = value.get("action_requests")
    raw_reviews = value.get("review_configs")
    if (
        not isinstance(raw_actions, list)
        or not raw_actions
        or not isinstance(raw_reviews, list)
        or len(raw_reviews) != len(raw_actions)
    ):
        raise TraceCorruption(
            "Tool review action and review-config lists must be non-empty and aligned"
        )
    actions: list[tuple[str, dict[str, JsonValue]]] = []
    for action in raw_actions:
        if not isinstance(action, dict):
            raise TraceCorruption("Tool review actions must be objects")
        name = action.get("name")
        arguments = action.get("args")
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise TraceCorruption("Tool review actions require a name and object args")
        actions.append((name, arguments))

    completed_call_ids = {
        message.tool_call_id
        for message in messages
        if message.message_type == "tool" and message.tool_call_id is not None
    }
    unique: tuple[str, ...] | None = None
    work = 0
    for message in messages:
        # A completed historical call cannot be the proposal that produced the
        # currently pending interrupt. Keep unresolved messages available so
        # parallel review groups and subgraph scopes retain their exact IDs.
        calls = tuple(
            call for call in message.tool_calls if call.id not in completed_call_ids
        )
        if not calls:
            continue
        work += len(actions) * len(calls)
        if work > 100_000:
            raise TraceCorruption(
                "Tool review correlation exceeded its safe work budget"
            )
        count, candidate = _ordered_tool_call_match(tuple(actions), calls)
        if count == 0:
            continue
        if count > 1 or unique is not None:
            raise TraceCorruption(
                "Tool review actions match multiple Tool call sequences"
            )
        unique = candidate
    if unique is None:
        raise TraceCorruption("Tool review actions do not match checkpoint Tool calls")
    return unique


def _ordered_tool_call_match(
    actions: tuple[tuple[str, dict[str, JsonValue]], ...],
    calls: tuple[NativeToolCall, ...],
) -> tuple[int, tuple[str, ...] | None]:
    """Count up to two ordered action matches and reconstruct the unique path."""

    normalized_calls = [(call.id, call.name, call.arguments) for call in calls]

    action_count = len(actions)
    call_count = len(normalized_calls)
    counts = [[0] * (call_count + 1) for _ in range(action_count + 1)]
    for call_index in range(call_count + 1):
        counts[action_count][call_index] = 1
    for action_index in range(action_count - 1, -1, -1):
        action_name, action_args = actions[action_index]
        for call_index in range(call_count - 1, -1, -1):
            total = counts[action_index][call_index + 1]
            _call_id, call_name, call_args = normalized_calls[call_index]
            if call_name == action_name and call_args == action_args:
                total += counts[action_index + 1][call_index + 1]
            counts[action_index][call_index] = min(2, total)
    match_count = counts[0][0]
    if match_count != 1:
        return match_count, None

    selected: list[str] = []
    action_index = 0
    call_index = 0
    while action_index < action_count:
        if call_index >= call_count:
            raise TraceCorruption("Unique Tool review correlation became incomplete")
        action_name, action_args = actions[action_index]
        call_id, call_name, call_args = normalized_calls[call_index]
        take = (
            call_name == action_name
            and call_args == action_args
            and counts[action_index + 1][call_index + 1] > 0
        )
        skip = counts[action_index][call_index + 1] > 0
        if take and skip:
            raise TraceCorruption("Unique Tool review correlation became ambiguous")
        if take:
            selected.append(call_id)
            action_index += 1
        elif not skip:
            raise TraceCorruption("Unique Tool review correlation became incomplete")
        call_index += 1
    return 1, tuple(selected)


__all__ = ["Tracer"]
