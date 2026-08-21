"""Validated stream contracts, correlation state, and HITL preparation."""

from __future__ import annotations

__all__ = [
    "_buffer_child_interrupts",
    "_build_response_schema",
    "_group_prepared_interrupts",
    "_interrupt_value_json",
    "_prepare_ag_ui_interrupts",
    "_prepare_root_interrupts",
    "_record_interrupts",
    "_tool_call_id_groups_for_actions",
    "_validate_prepared_interrupts",
]

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, cast

from ag_ui.core import Interrupt as AgUiInterrupt
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from .errors import HitlCorrelationError, HitlNoMatchError
from .hitl import (
    HitlActionRequest,
    HitlRequest,
    HitlToolCallCandidate,
    match_hitl_action_groups,
    match_hitl_tool_call_id_groups,
    relevant_malformed_hitl_candidate,
)
from .models import AgentRuntimeInterrupt, JsonObject, to_json_value
from .reasoning import (
    json_values_equal,
    normalize_operational_data,
    sanitize_public_data,
)
from .runtime_interrupts import (
    RuntimeInterruptEnvelope,
    parse_runtime_interrupt,
    prepare_runtime_ag_ui_interrupt,
)

if TYPE_CHECKING:
    from .adapter import DeepAgentAgUiAdapter

_MESSAGE_STATE_KEY = "messages"
StreamMode = Literal[
    "values",
    "updates",
    "checkpoints",
    "tasks",
    "debug",
    "messages",
    "custom",
]


class _ProtocolModel(BaseModel):
    """Provide shared validation behavior at the protocol boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
    )


def _to_json_value(value: object) -> JsonValue:
    """Convert framework objects into values accepted by Pydantic `JsonValue`."""

    return to_json_value(value)


class StreamMetadata(_ProtocolModel):
    """Metadata fields used to classify a Deep Agents message part."""

    model_config = ConfigDict(extra="allow")

    lc_agent_name: str | None = Field(
        default=None, description="Agent name recorded by LangChain"
    )
    langgraph_node: str | None = Field(
        default=None, description="LangGraph node that emitted the current event"
    )


class MessageStreamData(_ProtocolModel):
    """Validated payload of a v2 `messages` stream part."""

    message: BaseMessage = Field(description="Message object from the stream output")
    metadata: StreamMetadata = Field(
        description="LangGraph metadata associated with the message"
    )

    @field_validator("message", mode="before")
    @classmethod
    def validate_native_message(cls, value: object) -> object:
        """Require a supported live LangChain message rather than serialized data."""

        if not isinstance(value, (AIMessage, ToolMessage)):
            raise PydanticCustomError(
                "messages_native_object",
                "messages stream data must contain a live AIMessage or ToolMessage",
            )
        return value


class MessageStreamPart(_ProtocolModel):
    """Validated v2 `messages` envelope consumed by the adapter."""

    type: Literal["messages"] = Field(description="Stream-part type, fixed to messages")
    ns: tuple[str, ...] = Field(
        strict=True,
        description="Subgraph namespace that owns this stream part",
    )
    data: MessageStreamData = Field(description="Message stream-part payload")

    @field_validator("data", mode="before")
    @classmethod
    def validate_message_data(cls, value: object) -> object:
        """Name and validate the native message-and-metadata pair."""

        if not isinstance(value, tuple):
            raise PydanticCustomError(
                "messages_data_tuple",
                "messages stream data must be a two-item tuple",
            )
        pair = cast(tuple[object, ...], value)
        if len(pair) != 2:
            raise ValueError("messages stream data must contain message and metadata")
        return {"message": pair[0], "metadata": pair[1]}


class ValuesStreamPart(_ProtocolModel):
    """Validated v2 `values` envelope consumed by the adapter."""

    type: Literal["values"] = Field(description="Stream-part type, fixed to values")
    ns: tuple[str, ...] = Field(
        strict=True,
        description="Subgraph namespace that owns this stream part",
    )
    data: dict[str, object] = Field(description="Native state object in this part")
    interrupts: tuple[AgentRuntimeInterrupt, ...] = Field(
        strict=True, description="Interrupts attached to this stream part"
    )

    @field_validator("data", mode="before")
    @classmethod
    def validate_state(cls, value: object) -> object:
        """Preserve message objects while requiring a mapping state boundary."""

        if not isinstance(value, Mapping):
            raise TypeError("values stream data must be a JSON object")
        return dict(cast(Mapping[object, object], value))


class TaskStartPayload(_ProtocolModel):
    """Validated start payload from the v2 `tasks` stream."""

    id: str = Field(min_length=1, description="Stable LangGraph task ID")
    name: str = Field(min_length=1, description="Graph node about to execute")
    input: object = Field(description="Native input passed to the graph node")
    triggers: tuple[str, ...] = Field(
        strict=True,
        description="Channels that triggered this task",
    )
    metadata: StreamMetadata | None = Field(
        default=None,
        description="User-level task metadata parsed by LangGraph",
    )


class TaskResultPayload(_ProtocolModel):
    """Validated result payload from the v2 `tasks` stream."""

    id: str = Field(
        min_length=1,
        description="LangGraph task ID matching the corresponding start",
    )
    name: str = Field(min_length=1, description="Graph node that completed")
    error: object | None = Field(
        default=None, description="Native exception or error text from task failure"
    )
    interrupts: list[object] = Field(
        strict=True,
        description="Native task interrupts; values remains authoritative for state",
    )
    result: dict[str, object] = Field(
        description="Channels written by the task; values remains authoritative for state"
    )


TaskStreamPayload = TaskStartPayload | TaskResultPayload


class TasksStreamPart(_ProtocolModel):
    """Validated v2 `tasks` envelope consumed by the adapter."""

    type: Literal["tasks"] = Field(description="Stream-part type, fixed to tasks")
    ns: tuple[str, ...] = Field(
        strict=True,
        description="Parent graph namespace that owns this task",
    )
    data: TaskStreamPayload = Field(description="Task start or task result payload")


class ExtraStreamPart(_ProtocolModel):
    """Validated v2 envelope for modes projected as safe AG-UI RAW events."""

    type: Literal["updates", "checkpoints", "debug", "custom"] = Field(
        description="Native stream mode projected as a RAW event"
    )
    ns: tuple[str, ...] = Field(
        strict=True,
        description="Full graph namespace for the native stream part",
    )
    data: object = Field(description="Native payload validated before publication")


class NativeToolCall(_ProtocolModel):
    """Native Tool call projected from `tasks.data.input`."""

    name: str = Field(min_length=1, description="Tool name")
    args: JsonObject = Field(
        description="Tool arguments aggregated and validated as a JSON object"
    )
    id: str = Field(min_length=1, description="Model-generated Tool call ID")
    type: Literal["tool_call"] = Field(description="LangChain Tool call type")


class NativeToolCallList(RootModel[list[NativeToolCall]]):
    """Tool calls carried by one ToolNode task-start payload."""


DeepAgentStreamPart = Annotated[
    MessageStreamPart | TasksStreamPart | ValuesStreamPart | ExtraStreamPart,
    Field(discriminator="type"),
]


class DeepAgentStreamPartEnvelope(RootModel[DeepAgentStreamPart]):
    """Discriminated validation boundary for an untrusted v2 stream part."""


class AgentSource(_ProtocolModel):
    """Graph-scope identity attached to converted AG-UI events."""

    kind: Literal["root", "compiled_subgraph", "deep_agent_subagent"] = Field(
        description="Verified source kind for the complete graph namespace"
    )
    namespace: tuple[str, ...] = Field(
        description="Complete root or subgraph namespace"
    )
    parent_namespace: tuple[str, ...] | None = Field(
        default=None,
        description="Parent namespace that started this compiled graph task",
    )
    graph_task_id: str | None = Field(
        default=None, description="Full LangGraph task ID that opened this scope"
    )
    node_name: str | None = Field(
        default=None, description="Graph node whose task opened this scope"
    )
    agent_type: Literal["main", "subagent"] | None = Field(
        default=None,
        description="Agent role only for the root or a verified Deep Agents delegate",
    )
    agent_name: str | None = Field(
        default=None,
        min_length=1,
        description="Agent name only for the root or a verified Deep Agents delegate",
    )
    parent_tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        description="Scoped parent Tool call ID for a Deep Agents delegate",
    )
    subagent_input: str | None = Field(
        default=None,
        description="Complete task description supplied to a Deep Agents delegate",
    )


class EventContext(_ProtocolModel):
    """Serializable graph provenance written to AG-UI `rawEvent`."""

    stream_mode: StreamMode = Field(description="Native stream mode for this event")
    source: AgentSource = Field(description="Event source identity")
    run_id: str = Field(min_length=1, description="Main AG-UI run ID for the request")
    related_namespace: tuple[str, ...] | None = Field(
        default=None, description="Complete native namespace related to the event"
    )
    parent_tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        description="Parent task Tool call ID that started the current subagent",
    )
    langgraph_node: str | None = Field(
        default=None, description="LangGraph node associated with the event"
    )
    interrupt_id: str | None = Field(
        default=None, description="Interrupt ID when the event concerns a review"
    )
    tool_result_status: Literal["success", "error"] | None = Field(
        default=None,
        description="Explicit Tool execution status from ToolMessage",
    )


class ActiveToolCall(_ProtocolModel):
    """Correlation state for one streamed model Tool call."""

    tool_call_id: str = Field(min_length=1, description="Tool call ID")
    tool_name: str = Field(min_length=1, description="Tool name")
    parent_message_id: str | None = Field(
        default=None, description="Parent message ID that proposed the Tool call"
    )
    namespace: tuple[str, ...] = Field(description="Namespace of the Tool call")
    index: int | None = Field(
        default=None, description="Tool call position within the current message"
    )
    arguments: str = Field(default="", description="Accumulated Tool argument text")


class ActiveReasoning(_ProtocolModel):
    """Correlation state for one visible reasoning stream."""

    run_id: str = Field(min_length=1, description="Owning run ID")
    source_message_id: str = Field(
        min_length=1, description="Source message ID that started the reasoning stream"
    )
    reasoning_id: str = Field(min_length=1, description="AG-UI reasoning phase ID")
    message_id: str = Field(min_length=1, description="AG-UI reasoning message ID")
    namespace: tuple[str, ...] = Field(description="Reasoning stream namespace")


class SubagentInvocation(_ProtocolModel):
    """Subagent correlation established by a native task-start part."""

    parent_namespace: tuple[str, ...] = Field(
        description="Parent agent namespace that started the task"
    )
    child_namespace: tuple[str, ...] = Field(
        description="Child namespace derived from the graph task ID"
    )
    graph_task_id: str = Field(min_length=1, description="LangGraph task ID")
    parent_tool_call_id: str = Field(
        min_length=1, description="Parent agent task Tool call ID"
    )
    subagent_input: str = Field(description="Complete task.description content")
    agent_name: str = Field(
        min_length=1, description="Subagent name selected by task.subagent_type"
    )


@dataclass(frozen=True, slots=True)
class GraphScope:
    """Native compiled-graph scope established by a parent task start."""

    namespace: tuple[str, ...]
    parent_namespace: tuple[str, ...]
    graph_task_id: str
    node_name: str


@dataclass(frozen=True, slots=True)
class BufferedChildInterrupt:
    """Validated child interrupt awaiting an identical root propagation."""

    native_id: str
    value_json: str
    prepared: tuple[AgUiInterrupt, ...]
    namespaces: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class TaskStartFingerprint:
    """Normalized identity of one native task-start within its graph scope."""

    name: str
    input_json: str
    triggers: tuple[str, ...]
    metadata_json: str


@dataclass(frozen=True, slots=True)
class TaskResultFingerprint:
    """Canonical identity of one native task-result within its graph scope."""

    name: str
    error_json: str
    interrupts_json: str
    result_json: str


class ToolResultFingerprint(_ProtocolModel):
    """Stable deduplication fingerprint for a published Tool result."""

    message_id: str = Field(
        min_length=1, description="Complete scoped result message ID"
    )
    tool_name: str = Field(min_length=1, description="Tool name")
    content: str = Field(description="Normalized result content preserving semantics")
    status: Literal["success", "error"] = Field(description="Native ToolMessage status")


class JsonPatchOperation(_ProtocolModel):
    """One RFC 6902 operation emitted in an AG-UI `STATE_DELTA`."""

    op: Literal["add", "remove", "replace"] = Field(description="Patch operation")
    path: str = Field(description="JSON Pointer path")
    value: JsonValue | None = Field(
        default=None, description="New value for add or replace operations"
    )


def _interrupt_value_json(interrupt: AgentRuntimeInterrupt) -> str:
    """Return a strict finite fingerprint for propagation comparisons."""

    return json.dumps(
        _to_json_value(interrupt.value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _group_prepared_interrupts(
    native_interrupts: Sequence[AgentRuntimeInterrupt],
    prepared: Sequence[AgUiInterrupt],
) -> tuple[tuple[AgUiInterrupt, ...], ...]:
    """Restore native interrupt groups after public multi-action expansion."""

    grouped: list[tuple[AgUiInterrupt, ...]] = []
    offset = 0
    for native in native_interrupts:
        try:
            request = HitlRequest.model_validate(native.value)
        except ValidationError:
            size = 1
        else:
            size = len(request.action_requests)
        group = tuple(prepared[offset : offset + size])
        if len(group) != size:
            raise RuntimeError("prepared interrupt grouping is inconsistent")
        grouped.append(group)
        offset += size
    if offset != len(prepared):
        raise RuntimeError("prepared interrupt grouping is inconsistent")
    return tuple(grouped)


def _buffer_child_interrupts(
    self: DeepAgentAgUiAdapter,
    part: ValuesStreamPart,
    source: AgentSource,
    raw_messages: object,
) -> None:
    """Validate and stage child interrupts until root propagation arrives."""

    native_by_id: dict[str, AgentRuntimeInterrupt] = {}
    new_interrupts: list[AgentRuntimeInterrupt] = []
    for native in part.interrupts:
        if native.id in native_by_id:
            raise HitlCorrelationError(f"duplicate child interrupt ID: {native.id}")
        native_by_id[native.id] = native
        existing = self._child_interrupts.get(native.id)
        if existing is None:
            new_interrupts.append(native)
            continue
        if existing.value_json != self._interrupt_value_json(native):
            raise HitlCorrelationError(
                f"conflicting child interrupt propagation for ID: {native.id}"
            )
        if part.ns not in existing.namespaces and any(
            not (
                namespace == part.ns[: len(namespace)]
                or part.ns == namespace[: len(part.ns)]
            )
            for namespace in existing.namespaces
        ):
            raise HitlCorrelationError(
                "the same interrupt ID appeared in unrelated child namespaces: "
                f"{native.id}"
            )

    prepared_new = self._prepare_ag_ui_interrupts(
        tuple(new_interrupts),
        source,
        raw_messages,
    )
    grouped_new = self._group_prepared_interrupts(
        new_interrupts,
        prepared_new,
    )
    prepared_by_native_id = {
        native.id: group
        for native, group in zip(new_interrupts, grouped_new, strict=True)
    }
    converted_messages = tuple(self._convert_messages(raw_messages, part.ns))

    staged_interrupts = dict(self._child_interrupts)
    for native in part.interrupts:
        existing = staged_interrupts.get(native.id)
        if existing is None:
            staged_interrupts[native.id] = BufferedChildInterrupt(
                native_id=native.id,
                value_json=self._interrupt_value_json(native),
                prepared=prepared_by_native_id[native.id],
                namespaces=(part.ns,),
            )
        elif part.ns not in existing.namespaces:
            staged_interrupts[native.id] = BufferedChildInterrupt(
                native_id=existing.native_id,
                value_json=existing.value_json,
                prepared=existing.prepared,
                namespaces=(*existing.namespaces, part.ns),
            )

    self._validate_prepared_interrupts(
        [
            public
            for buffered in staged_interrupts.values()
            for public in buffered.prepared
        ]
    )
    native_ids = tuple(native_by_id)
    previous_ids = self._child_interrupt_ids_by_namespace.get(part.ns)
    if previous_ids is not None and previous_ids != native_ids:
        raise HitlCorrelationError(
            f"conflicting child interrupt set for namespace: {part.ns!r}"
        )
    previous_messages = self._child_message_snapshots.get(part.ns)
    if previous_ids is not None and previous_messages != converted_messages:
        raise HitlCorrelationError(
            f"conflicting child message snapshot for namespace: {part.ns!r}"
        )

    staged_ids_by_namespace = dict(self._child_interrupt_ids_by_namespace)
    staged_ids_by_namespace[part.ns] = native_ids
    staged_messages = dict(self._child_message_snapshots)
    staged_messages[part.ns] = converted_messages
    self._child_interrupts = staged_interrupts
    self._child_interrupt_ids_by_namespace = staged_ids_by_namespace
    self._child_message_snapshots = staged_messages


def _prepare_root_interrupts(
    self: DeepAgentAgUiAdapter,
    native_interrupts: Sequence[AgentRuntimeInterrupt],
    source: AgentSource,
    raw_messages: object,
) -> tuple[list[AgUiInterrupt], set[str], tuple[tuple[str, ...], ...]]:
    """Match root interrupts to buffered children and prepare root-local ones."""

    native_by_id: dict[str, AgentRuntimeInterrupt] = {}
    for native in native_interrupts:
        if native.id in native_by_id:
            raise HitlCorrelationError(f"duplicate root interrupt ID: {native.id}")
        native_by_id[native.id] = native
        buffered = self._child_interrupts.get(native.id)
        if buffered is not None and buffered.value_json != self._interrupt_value_json(
            native
        ):
            raise HitlCorrelationError(
                f"conflicting root propagation for child interrupt ID: {native.id}"
            )

    unresolved = set(self._child_interrupts) - self._resolved_child_interrupt_ids
    missing = unresolved - set(native_by_id)
    if missing:
        raise HitlCorrelationError(
            "root values did not propagate child interrupts: "
            f"{', '.join(sorted(missing))}"
        )

    root_local = tuple(
        native
        for native in native_interrupts
        if native.id not in self._child_interrupts
    )
    prepared_local = self._prepare_ag_ui_interrupts(
        root_local,
        source,
        raw_messages,
    )
    grouped_local = self._group_prepared_interrupts(
        root_local,
        prepared_local,
    )
    local_by_id = {
        native.id: group
        for native, group in zip(root_local, grouped_local, strict=True)
    }
    prepared: list[AgUiInterrupt] = []
    propagated_child_ids: set[str] = set()
    for native in native_interrupts:
        buffered = self._child_interrupts.get(native.id)
        if buffered is None:
            prepared.extend(local_by_id[native.id])
        else:
            prepared.extend(buffered.prepared)
            propagated_child_ids.add(native.id)
    self._validate_prepared_interrupts(prepared)

    child_namespaces = tuple(
        namespace
        for namespace in self._graph_scopes
        if propagated_child_ids.intersection(
            self._child_interrupt_ids_by_namespace.get(namespace, ())
        )
    )
    return prepared, propagated_child_ids, child_namespaces


def _record_interrupts(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    interrupts: Sequence[AgUiInterrupt],
) -> None:
    """Record terminal interrupts in first-seen order and ignore replayed frames."""

    for interrupt in interrupts:
        if interrupt.id in self._interrupts_by_id:
            continue
        self._interrupts_by_id[interrupt.id] = interrupt
        self._interrupts_in_order.append(interrupt)


def _prepare_ag_ui_interrupts(
    self: DeepAgentAgUiAdapter,
    native_interrupts: Sequence[AgentRuntimeInterrupt],
    source: AgentSource,
    raw_messages: object,
) -> list[AgUiInterrupt]:
    """Validate all HITL Tool correlations before mutating adapter state."""

    if not native_interrupts:
        return []
    parsed: list[
        tuple[
            AgentRuntimeInterrupt,
            HitlRequest | None,
            RuntimeInterruptEnvelope | None,
        ]
    ] = []
    action_groups: list[Sequence[HitlActionRequest]] = []
    for interrupt in native_interrupts:
        try:
            runtime_envelope = parse_runtime_interrupt(interrupt.value)
        except ValidationError as error:
            raise HitlCorrelationError(
                f"invalid TinkerFin runtime interrupt: {interrupt.id}"
            ) from error
        if runtime_envelope is not None:
            parsed.append((interrupt, None, runtime_envelope))
            continue
        try:
            request = HitlRequest.model_validate(interrupt.value)
        except ValidationError as error:
            value = interrupt.value
            if isinstance(value, dict) and (
                "action_requests" in value or "review_configs" in value
            ):
                raise HitlCorrelationError(
                    f"invalid Deep Agents HITL interrupt: {interrupt.id}"
                ) from error
            request = None
        parsed.append((interrupt, request, None))
        if request is not None:
            action_groups.append(request.action_requests)

    if any(request is not None for _, request, _ in parsed) and any(
        runtime is not None for _, _, runtime in parsed
    ):
        raise HitlCorrelationError(
            "runtime and Deep Agents Tool interrupts cannot share one batch"
        )

    matched_id_groups = self._tool_call_id_groups_for_actions(
        source.namespace,
        action_groups,
        raw_messages,
    )
    matched_group_index = 0
    prepared: list[AgUiInterrupt] = []
    for interrupt, request, runtime_envelope in parsed:
        if runtime_envelope is not None:
            prepared.append(
                prepare_runtime_ag_ui_interrupt(
                    interrupt,
                    runtime_envelope,
                    source=source.model_dump(mode="json", by_alias=True),
                )
            )
            continue
        if request is None:
            prepared.append(
                AgUiInterrupt(
                    id=interrupt.id,
                    reason="langgraph_interrupt",
                    metadata={
                        "langgraphValue": sanitize_public_data(interrupt.value),
                        "source": source.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
            )
            continue
        group_ids = matched_id_groups[matched_group_index]
        matched_group_index += 1
        public_langgraph_value = sanitize_public_data(interrupt.value)
        if not isinstance(public_langgraph_value, dict):
            raise TypeError("HITL interrupt value must be a JSON object")
        public_actions = public_langgraph_value.get("action_requests")
        if not isinstance(public_actions, list) or len(public_actions) != len(
            request.action_requests
        ):
            raise HitlCorrelationError(
                f"invalid Deep Agents HITL interrupt: {interrupt.id}"
            )
        for public_action, action in zip(
            public_actions,
            request.action_requests,
            strict=True,
        ):
            if not isinstance(public_action, dict):
                raise HitlCorrelationError(
                    f"invalid Deep Agents HITL interrupt: {interrupt.id}"
                )
            public_action["args"] = normalize_operational_data(action.args.root)
        multi_action = len(request.action_requests) > 1
        for index, (action, review, tool_call_id) in enumerate(
            zip(
                request.action_requests,
                request.review_configs,
                group_ids,
                strict=True,
            )
        ):
            public_id = f"{interrupt.id}#{index}" if multi_action else interrupt.id
            prepared.append(
                AgUiInterrupt(
                    id=public_id,
                    reason="tool_call",
                    message=(action.description or f"Approve tool {action.name}"),
                    tool_call_id=tool_call_id,
                    response_schema=self._build_response_schema(
                        review.allowed_decisions,
                        action_name=action.name,
                        args_schema=(
                            None
                            if review.args_schema is None
                            else review.args_schema.root
                        ),
                    ),
                    metadata={
                        # A validated HITL payload is operational Tool data. Its
                        # args and args_schema must match the proposal exactly.
                        "langgraphValue": public_langgraph_value,
                        "source": source.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                        "deepagents": {
                            "nativeInterruptId": interrupt.id,
                            "actionIndex": index,
                            "toolName": action.name,
                            "allowedDecisions": list(review.allowed_decisions),
                            "originalArgs": action.args.root,
                        },
                    },
                )
            )
    self._validate_prepared_interrupts(prepared)
    return prepared


def _validate_prepared_interrupts(
    self: DeepAgentAgUiAdapter,
    prepared: Sequence[AgUiInterrupt],
) -> None:
    """Reject public-ID collisions and conflicting committed replays."""

    prepared_by_id: dict[str, AgUiInterrupt] = {}
    for interrupt in prepared:
        if interrupt.id in prepared_by_id:
            raise ValueError(f"duplicate public interrupt ID: {interrupt.id}")
        prepared_by_id[interrupt.id] = interrupt
        previous = self._interrupts_by_id.get(interrupt.id)
        if previous is not None and not json_values_equal(
            _to_json_value(
                previous.model_dump(
                    mode="python",
                    by_alias=True,
                    exclude_none=False,
                )
            ),
            _to_json_value(
                interrupt.model_dump(
                    mode="python",
                    by_alias=True,
                    exclude_none=False,
                )
            ),
        ):
            raise ValueError(f"conflicting interrupt replay for ID: {interrupt.id}")


def _tool_call_id_groups_for_actions(
    self: DeepAgentAgUiAdapter,
    namespace: tuple[str, ...],
    action_groups: Sequence[Sequence[HitlActionRequest]],
    raw_messages: object,
) -> list[list[str]]:
    """Require each action group to have one unique Tool-call assignment."""

    if not action_groups:
        return []

    if isinstance(raw_messages, Sequence) and not isinstance(
        raw_messages,
        (str, bytes),
    ):
        messages = cast(Sequence[object], raw_messages)
        checkpoint_messages = tuple(
            message for message in messages if isinstance(message, BaseMessage)
        )
        if any(
            isinstance(message, AIMessage) and message.tool_calls
            for message in checkpoint_messages
        ):
            raw_groups = match_hitl_tool_call_id_groups(
                action_groups,
                checkpoint_messages,
            )
            return [
                [self._tool_call_id(namespace, raw_id) for raw_id in raw_ids]
                for raw_ids in raw_groups
            ]

    calls_by_message: dict[str, list[ActiveToolCall]] = {}
    seen_tool_ids: set[str] = set()
    for history in self._tool_history.values():
        for call in history:
            if (
                call.namespace != namespace
                or call.tool_call_id in seen_tool_ids
                or call.parent_message_id is None
            ):
                continue
            seen_tool_ids.add(call.tool_call_id)
            calls_by_message.setdefault(call.parent_message_id, []).append(call)
    candidate_messages: list[list[HitlToolCallCandidate]] = []
    for calls in calls_by_message.values():
        candidates: list[HitlToolCallCandidate] = []
        for call in sorted(
            calls,
            key=lambda item: (
                item.index is None,
                item.index if item.index is not None else 0,
            ),
        ):
            if call.index is None or call.parent_message_id is None:
                raise HitlCorrelationError(
                    "streamed Tool call history requires message positions"
                )
            arguments: JsonValue | None
            arguments_error: json.JSONDecodeError | None
            try:
                arguments = normalize_operational_data(
                    json.loads(call.arguments or "{}")
                )
            except json.JSONDecodeError as error:
                arguments = None
                arguments_error = error
            else:
                arguments_error = None
            candidates.append(
                HitlToolCallCandidate(
                    namespace=call.namespace,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    parent_message_id=call.parent_message_id,
                    position=call.index,
                    arguments=arguments,
                    arguments_error=arguments_error,
                )
            )
        candidate_messages.append(candidates)
    try:
        matched_groups = match_hitl_action_groups(
            action_groups,
            candidate_messages,
        )
    except HitlNoMatchError:
        malformed = relevant_malformed_hitl_candidate(
            action_groups,
            candidate_messages,
        )
        if malformed is None or malformed.arguments_error is None:
            raise
        raise HitlCorrelationError(
            "streamed Tool call history contains invalid JSON arguments"
        ) from malformed.arguments_error
    malformed = relevant_malformed_hitl_candidate(
        action_groups,
        candidate_messages,
    )
    if malformed is not None and malformed.arguments_error is not None:
        raise HitlCorrelationError(
            "streamed Tool call history contains invalid JSON arguments"
        ) from malformed.arguments_error
    return [list(group) for group in matched_groups]


def _build_response_schema(
    allowed_decisions: Sequence[str],
    *,
    action_name: str,
    args_schema: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """Build an AG-UI resume payload schema that fixes the original Tool identity."""

    variants: list[dict[str, object]] = []
    for decision_type in allowed_decisions:
        properties: dict[str, object] = {
            "type": {"const": decision_type},
        }
        required = ["type"]
        if decision_type == "edit":
            properties["edited_action"] = {
                "type": "object",
                "required": ["name", "args"],
                "properties": {
                    "name": {"const": action_name},
                    "args": (
                        {"type": "object"} if args_schema is None else args_schema
                    ),
                },
                "additionalProperties": False,
            }
            required.append("edited_action")
        elif decision_type == "reject":
            properties["message"] = {"type": "string"}
        elif decision_type == "respond":
            properties["message"] = {"type": "string"}
            required.append("message")
        variants.append(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )
    return JsonObject.model_validate({"oneOf": variants}).root
