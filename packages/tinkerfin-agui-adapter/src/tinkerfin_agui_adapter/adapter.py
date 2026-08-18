"""Stateful conversion of Deep Agents v2 stream parts to AG-UI events."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from ag_ui.core import (
    AssistantMessage,
    BaseEvent,
    MessagesSnapshotEvent,
    RawEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCall,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
    UserMessage,
)
from ag_ui.core import Interrupt as AgUiInterrupt
from ag_ui.core import SystemMessage as AgUiSystemMessage
from ag_ui.core import ToolMessage as AgUiToolMessage
from ag_ui.core.types import FunctionCall, Message
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallChunk,
    ToolMessage,
)
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

from .contracts import AgentRunOutcome
from .hitl import (
    HitlActionRequest,
    HitlCorrelationError,
    HitlNoMatchError,
    HitlRequest,
    HitlToolCallCandidate,
    match_hitl_action_groups,
    match_hitl_tool_call_id_groups,
    relevant_malformed_hitl_candidate,
)
from .ids import ScopedIdCodec
from .models import (
    AgentRuntimeInterrupt,
    JsonObject,
    to_json_value,
)
from .reasoning import (
    json_values_equal,
    normalize_operational_data,
    sanitize_public_data,
)
from .subagent import SubagentTaskInput

_MESSAGE_STATE_KEY = "messages"
InterruptCorrelationError = HitlCorrelationError
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
        if len(value) != 2:
            raise ValueError("messages stream data must contain message and metadata")
        return {"message": value[0], "metadata": value[1]}


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
        return dict(value)


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


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_patch(
    previous: JsonValue,
    current: JsonValue,
    path: str = "",
) -> list[JsonPatchOperation]:
    """Build a deterministically ordered RFC 6902 patch between two JSON values."""

    if json_values_equal(previous, current):
        return []
    if isinstance(previous, dict) and isinstance(current, dict):
        operations: list[JsonPatchOperation] = []
        for key in sorted(previous.keys() - current.keys()):
            operations.append(
                JsonPatchOperation(
                    op="remove",
                    path=f"{path}/{_escape_json_pointer(key)}",
                )
            )
        for key in sorted(current.keys() - previous.keys()):
            operations.append(
                JsonPatchOperation(
                    op="add",
                    path=f"{path}/{_escape_json_pointer(key)}",
                    value=current[key],
                )
            )
        for key in sorted(previous.keys() & current.keys()):
            operations.extend(
                _json_patch(
                    previous[key],
                    current[key],
                    f"{path}/{_escape_json_pointer(key)}",
                )
            )
        return operations
    return [JsonPatchOperation(op="replace", path=path, value=current)]


def _validate_text_content_blocks(content: object) -> None:
    """Reject text blocks whose required payload is absent or not a string."""

    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        if not isinstance(block.get("text"), str):
            raise TypeError("text content blocks require a string 'text' field")


def _normalize_tool_content(content: object) -> str:
    """Normalize Tool output according to the AG-UI adapter contract."""

    _validate_text_content_blocks(content)

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(cast(str, block["text"]))
            else:
                parts.append(json.dumps(_to_json_value(block), ensure_ascii=False))
        return "".join(parts)
    return json.dumps(_to_json_value(content), ensure_ascii=False)


def _visible_ai_content(message: AIMessage) -> str:
    """Serialize AI content without inferring provider privacy from domain fields.

    LangChain's ordinary text projection intentionally flattens text blocks, while
    AG-UI message snapshots have only a string content field. Preserve a non-text
    content graph as one JSON value so domain blocks are not silently discarded or
    unwrapped merely because their ``type`` resembles provider reasoning.
    """

    if isinstance(message.content, list):
        has_non_text_block = any(
            not (
                isinstance(block, str)
                or (isinstance(block, Mapping) and block.get("type") == "text")
            )
            for block in message.content
        )
        if has_non_text_block:
            _validate_text_content_blocks(message.content)
            return json.dumps(
                sanitize_public_data(message.content),
                ensure_ascii=False,
            )
    return _normalize_tool_content(message.content)


def _complete_ai_message_to_chunk(message: AIMessage) -> AIMessageChunk:
    """Normalize a complete non-streaming AIMessage into one final chunk."""

    tool_call_chunks = [
        ToolCallChunk(
            name=tool_call["name"],
            args=json.dumps(_to_json_value(tool_call["args"]), ensure_ascii=False),
            id=tool_call["id"],
            index=index,
            type="tool_call_chunk",
        )
        for index, tool_call in enumerate(message.tool_calls)
    ]
    return AIMessageChunk(
        content=message.content,
        additional_kwargs=message.additional_kwargs,
        response_metadata=message.response_metadata,
        name=message.name,
        id=message.id,
        usage_metadata=message.usage_metadata,
        tool_call_chunks=tool_call_chunks,
        chunk_position="last",
    )


def _public_task_error(error: object | None) -> object | None:
    """Project a native task error into a stable public shape without traceback."""

    if isinstance(error, BaseException):
        return {"type": type(error).__name__, "message": str(error)}
    return error


def _safe_checkpoint_task(value: object) -> dict[str, JsonValue]:
    """Project one checkpoint task without its resumable runtime state."""

    if not isinstance(value, Mapping):
        raise TypeError("checkpoint tasks must be JSON objects")
    allowed = ("id", "name", "result", "interrupts")
    projected = {key: value[key] for key in allowed if key in value}
    if "error" in value:
        projected["error"] = _public_task_error(value["error"])
    normalized = sanitize_public_data(projected)
    if not isinstance(normalized, dict):
        raise TypeError("checkpoint task projection must be a JSON object")
    return normalized


def _safe_debug_task_start(value: object) -> dict[str, JsonValue]:
    """Project a debug task start without runtime configuration metadata."""

    if not isinstance(value, Mapping):
        raise TypeError("debug task data must be a JSON object")
    projected = {
        key: value[key] for key in ("id", "name", "input", "triggers") if key in value
    }
    normalized = sanitize_public_data(projected)
    if not isinstance(normalized, dict):
        raise TypeError("debug task projection must be a JSON object")
    return normalized


def _safe_checkpoint_payload(value: object) -> JsonValue:
    """Remove checkpoint configuration from checkpoint and debug payloads."""

    if not isinstance(value, Mapping):
        raise TypeError("checkpoint and debug stream data must be JSON objects")
    if "type" in value or "payload" in value:
        debug_type = value.get("type")
        if debug_type not in {"checkpoint", "task", "task_result"}:
            raise ValueError("debug stream data has an unsupported event type")
        payload = value.get("payload")
        if debug_type == "checkpoint":
            safe_payload = _safe_checkpoint_snapshot(payload)
        elif debug_type == "task":
            safe_payload = _safe_debug_task_start(payload)
        else:
            safe_payload = _safe_checkpoint_task(payload)
        normalized = sanitize_public_data(
            {key: value[key] for key in ("step", "timestamp", "type") if key in value}
            | {"payload": safe_payload}
        )
        if not isinstance(normalized, dict):
            raise TypeError("debug stream projection must be a JSON object")
        return normalized
    return _safe_checkpoint_snapshot(value)


def _safe_checkpoint_snapshot(value: object) -> dict[str, JsonValue]:
    """Whitelist stable checkpoint fields and sanitize nested task results."""

    if not isinstance(value, Mapping):
        raise TypeError("checkpoint stream data must be a JSON object")
    projected: dict[str, object] = {
        key: value[key] for key in ("values", "next") if key in value
    }
    metadata = value.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise TypeError("checkpoint metadata must be a JSON object")
        if "step" in metadata:
            projected["step"] = metadata["step"]
    if "tasks" in value:
        tasks = value["tasks"]
        if not isinstance(tasks, Sequence) or isinstance(
            tasks, (str, bytes, bytearray)
        ):
            raise TypeError("checkpoint tasks must be a sequence")
        projected["tasks"] = [_safe_checkpoint_task(task) for task in tasks]
    normalized = sanitize_public_data(projected)
    if not isinstance(normalized, dict):
        raise TypeError("checkpoint stream projection must be a JSON object")
    return normalized


class DeepAgentAgUiAdapter:
    """Convert individual Deep Agents v2 stream parts into AG-UI events.

    Each instance belongs to one caller-declared main run and preserves event
    order and full namespace-scoped identifiers across `messages`, `tasks`, and
    `values` parts. It supports ordinary compiled subgraphs as native graph
    scopes and enriches only verified Deep Agents `task` delegates with
    subagent identity, input, and parent Tool provenance.

    `process()` validates a complete part before mutating correlation state and
    propagates validation or correlation errors unchanged. `finish()` and
    `abort()` close open reasoning, text, and Tool lifecycles idempotently, but
    the caller owns the main `RUN_STARTED` and terminal events.

    The adapter owns no graph, checkpointer, transport, or server resource. The
    verified private provider metadata path is
    removed from public payloads regardless of `expose_reasoning_events`; that flag
    controls only emission from the adapter's supported reasoning sources and is
    not permission to expose raw provider payloads.
    """

    def __init__(
        self,
        run_id: str,
        *,
        prior_tool_call_ids: frozenset[str] = frozenset(),
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
    ) -> None:
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        if not run_id or run_id != run_id.strip():
            raise ValueError("run_id must be non-blank without surrounding whitespace")
        if not isinstance(expose_reasoning_events, bool):
            raise TypeError("expose_reasoning_events must be a bool")
        if not isinstance(expose_subagent_events, bool):
            raise TypeError("expose_subagent_events must be a bool")
        self.run_id = run_id
        self._expose_reasoning_events = expose_reasoning_events
        self._expose_subagent_events = expose_subagent_events
        self._ids = ScopedIdCodec()
        self._active_messages: dict[tuple[str, ...], str] = {}
        self._active_reasoning: dict[tuple[str, str], ActiveReasoning] = {}
        self._active_tools: dict[str, ActiveToolCall] = {}
        self._tool_history: dict[tuple[tuple[str, ...], str], list[ActiveToolCall]] = {}
        self._native_tool_calls: dict[
            tuple[tuple[str, ...], str], list[NativeToolCall]
        ] = {}
        self._tool_ids_by_index: dict[tuple[tuple[str, ...], str, int | None], str] = {}
        self._tool_id_history_by_index: dict[
            tuple[tuple[str, ...], str, int | None], str
        ] = {}
        self._tool_names_by_id: dict[str, str] = {}
        self._prior_tool_call_ids: set[str] = set()
        self._started_tool_ids: set[str] = set()
        self._ended_tool_ids: set[str] = set()
        self._result_fingerprints: dict[str, ToolResultFingerprint] = {}
        if not isinstance(prior_tool_call_ids, frozenset):
            raise TypeError("prior_tool_call_ids must be a frozenset")
        for event_id in prior_tool_call_ids:
            try:
                kind, _namespace, _raw_tool_call_id = self._ids.decode(event_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "prior_tool_call_ids must contain complete scoped Tool IDs"
                ) from error
            if kind != "tool":
                raise ValueError(
                    "prior_tool_call_ids must contain complete scoped Tool IDs"
                )
            # The interrupt terminal already closed these proposals. Resume may emit
            # results only; unknown calls still receive a complete synthesized lifecycle.
            self._prior_tool_call_ids.add(event_id)
            self._started_tool_ids.add(event_id)
            self._ended_tool_ids.add(event_id)
        self._namespace_agent_names: dict[tuple[str, ...], str] = {}
        self._graph_scopes: dict[tuple[str, ...], GraphScope] = {}
        self._subagent_invocations: dict[tuple[str, ...], SubagentInvocation] = {}
        self._sub_namespaces_by_parent_tool_call: dict[
            tuple[tuple[str, ...], str], tuple[str, ...]
        ] = {}
        self._task_start_fingerprints: dict[
            tuple[tuple[str, ...], str], TaskStartFingerprint
        ] = {}
        self._task_result_fingerprints: dict[
            tuple[tuple[str, ...], str], TaskResultFingerprint
        ] = {}
        self._previous_root_state: dict[str, JsonValue] | None = None
        self._child_interrupts: dict[str, BufferedChildInterrupt] = {}
        self._resolved_child_interrupt_ids: set[str] = set()
        self._child_interrupt_ids_by_namespace: dict[
            tuple[str, ...], tuple[str, ...]
        ] = {}
        self._child_message_snapshots: dict[tuple[str, ...], tuple[Message, ...]] = {}
        self._interrupts_in_order: list[AgUiInterrupt] = []
        self._interrupts_by_id: dict[str, AgUiInterrupt] = {}

    def process(self, part: object) -> list[BaseEvent]:
        """Validate and convert one part without owning or advancing its source.

        Returns events in protocol order. Validation and correlation errors are
        raised before this part changes lifecycle state.
        """

        validated = DeepAgentStreamPartEnvelope.model_validate(part).root
        if isinstance(validated, MessageStreamPart):
            self._validate_message_part(validated)
            events = self._process_message_part(validated)
        elif isinstance(validated, TasksStreamPart):
            events = self._process_tasks_part(validated)
        elif isinstance(validated, ValuesStreamPart):
            events = self._process_values_part(validated)
        else:
            events = self._process_extra_part(validated)
        return self._visible_events(events)

    def _visible_events(self, events: list[BaseEvent]) -> list[BaseEvent]:
        """Suppress every event whose serialized provenance is a subgraph."""

        if self._expose_subagent_events:
            return events
        visible: list[BaseEvent] = []
        for event in events:
            raw_event = event.raw_event
            if isinstance(raw_event, Mapping):
                source = raw_event.get("source")
                if isinstance(source, Mapping) and source.get("namespace"):
                    continue
                namespace = raw_event.get("ns")
                if (
                    isinstance(namespace, Sequence)
                    and not isinstance(namespace, (str, bytes, bytearray))
                    and namespace
                ):
                    continue
            visible.append(event)
        return visible

    def _validate_message_part(self, part: MessageStreamPart) -> None:
        """Validate stable message and Tool-fragment IDs before mutating state."""

        message = part.data.message
        if not isinstance(message, AIMessage):
            return
        raw_message_id = self._stable_message_id(message)
        chunk = (
            message
            if isinstance(message, AIMessageChunk)
            else _complete_ai_message_to_chunk(message)
        )
        available_slots = dict(self._tool_id_history_by_index)
        slot_by_tool_id = {
            tool_call_id: index_key
            for index_key, tool_call_id in available_slots.items()
        }
        tool_names = dict(self._tool_names_by_id)
        parent_message_id = self._message_id(part.ns, raw_message_id)
        for tool_chunk in chunk.tool_call_chunks:
            index = tool_chunk.get("index")
            if type(index) is not int:
                raise ValueError("tool-call fragments require an integer index")
            index_key = (part.ns, parent_message_id, index)
            raw_tool_call_id = tool_chunk.get("id")
            tool_name = tool_chunk.get("name")
            if raw_tool_call_id is not None or tool_name is not None:
                if (
                    not isinstance(raw_tool_call_id, str)
                    or not raw_tool_call_id
                    or not isinstance(tool_name, str)
                    or not tool_name
                ):
                    raise ValueError("tool-call starts require both id and name")
                tool_call_id = self._tool_call_id(part.ns, raw_tool_call_id)
                if (
                    tool_call_id in self._ended_tool_ids
                    and tool_call_id not in self._prior_tool_call_ids
                ):
                    raise ValueError("an in-run ended tool-call ID cannot start again")
                previous_slot = slot_by_tool_id.get(tool_call_id)
                if previous_slot is not None and previous_slot != index_key:
                    raise ValueError("tool-call IDs cannot span multiple indices")
                previous_id = available_slots.get(index_key)
                if previous_id is not None and previous_id != tool_call_id:
                    raise ValueError("tool-call indices cannot change IDs")
                previous_name = tool_names.get(tool_call_id)
                if previous_name is not None and previous_name != tool_name:
                    raise ValueError("tool-call IDs cannot change names")
                tool_names[tool_call_id] = tool_name
                if tool_call_id not in self._ended_tool_ids:
                    available_slots[index_key] = tool_call_id
                    slot_by_tool_id[tool_call_id] = index_key
            elif index_key not in available_slots:
                raise ValueError("tool-call args fragment has no scoped start")
            args = tool_chunk.get("args")
            if args is not None and not isinstance(args, str):
                raise TypeError("tool-call args fragments must be strings")

    def finish(self) -> list[BaseEvent]:
        """Close open child lifecycles without emitting a main terminal event."""

        unresolved = set(self._child_interrupts) - self._resolved_child_interrupt_ids
        if unresolved:
            raise InterruptCorrelationError(
                "child interrupts were not propagated by root values: "
                f"{', '.join(sorted(unresolved))}"
            )
        events = self._close_all_reasoning()
        events.extend(self._close_all_messages())
        events.extend(self._close_all_tools())
        return self._visible_events(events)

    def abort(self, *, code: str) -> list[BaseEvent]:
        """Close open child lifecycles after failure, without a main terminal."""

        return self._visible_events(
            [
                *self._close_all_reasoning(),
                *self._close_all_messages(),
                *self._close_all_tools(),
            ]
        )

    def main_outcome(self) -> AgentRunOutcome:
        """Return success or the ordered interrupts observed at a values boundary."""

        if self._interrupts_in_order:
            return AgentRunOutcome(
                type="interrupt",
                interrupts=tuple(self._interrupts_in_order),
            )
        return AgentRunOutcome(type="success")

    def _process_message_part(self, part: MessageStreamPart) -> list[BaseEvent]:
        metadata = part.data.metadata
        agent_name = metadata.lc_agent_name
        source = self._source(part.ns)
        self._require_started_source(source)
        self._record_agent_name(part.ns, agent_name)
        source = self._source(part.ns)
        events: list[BaseEvent] = []
        message = part.data.message
        related_namespace: tuple[str, ...] | None = None
        if isinstance(message, ToolMessage):
            scoped_tool_call_id = self._tool_call_id(
                part.ns,
                str(message.tool_call_id),
            )
            correlated_name = message.name or self._tool_names_by_id.get(
                scoped_tool_call_id
            )
            if correlated_name == "task":
                related_namespace = self._sub_namespaces_by_parent_tool_call.get(
                    (part.ns, str(message.tool_call_id))
                )
        raw_event = self._event_context(
            "messages",
            source,
            langgraph_node=metadata.langgraph_node,
            related_namespace=related_namespace,
            tool_result_status=(
                message.status if isinstance(message, ToolMessage) else None
            ),
        )
        if isinstance(message, AIMessage):
            chunk = (
                message
                if isinstance(message, AIMessageChunk)
                else _complete_ai_message_to_chunk(message)
            )
            events.extend(self._process_ai_chunk(chunk, source, raw_event))
        elif isinstance(message, ToolMessage):
            events.extend(self._process_tool_result(message, source, raw_event))
        return events

    def _process_tasks_part(self, part: TasksStreamPart) -> list[BaseEvent]:
        """Publish task provenance and establish subgraph correlation on start."""

        if isinstance(part.data, TaskStartPayload):
            return self._process_task_start(part.ns, part.data)
        return self._process_task_result(part.ns, part.data)

    def _process_task_start(
        self,
        parent_namespace: tuple[str, ...],
        payload: TaskStartPayload,
    ) -> list[BaseEvent]:
        parent_source = self._source(parent_namespace)
        self._require_started_source(parent_source)
        tool_calls = (
            NativeToolCallList.model_validate(payload.input).root
            if payload.name == "tools"
            else []
        )
        fingerprint = TaskStartFingerprint(
            name=payload.name,
            input_json=json.dumps(
                _to_json_value(
                    [
                        tool_call.model_dump(mode="json", by_alias=True)
                        for tool_call in tool_calls
                    ]
                    if payload.name == "tools"
                    else payload.input
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            triggers=payload.triggers,
            metadata_json=json.dumps(
                (
                    None
                    if payload.metadata is None
                    else _to_json_value(
                        payload.metadata.model_dump(
                            mode="python",
                            by_alias=False,
                            exclude_none=False,
                        )
                    )
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        fingerprint_key = (parent_namespace, payload.id)
        existing_fingerprint = self._task_start_fingerprints.get(fingerprint_key)
        if existing_fingerprint is not None and existing_fingerprint != fingerprint:
            raise ValueError(
                "conflicting task start for "
                f"namespace={parent_namespace!r} graph_task_id={payload.id!r}"
            )
        subagents: list[dict[str, JsonValue]] = []
        staged_native_tool_calls: dict[
            tuple[tuple[str, ...], str], list[NativeToolCall]
        ] = {key: list(calls) for key, calls in self._native_tool_calls.items()}
        staged_graph_scopes = dict(self._graph_scopes)
        staged_subagent_invocations = dict(self._subagent_invocations)
        staged_sub_namespaces = dict(self._sub_namespaces_by_parent_tool_call)
        staged_agent_names = dict(self._namespace_agent_names)
        staged_tool_names = dict(self._tool_names_by_id)
        child_namespace = (*parent_namespace, f"{payload.name}:{payload.id}")
        graph_scope = GraphScope(
            namespace=child_namespace,
            parent_namespace=parent_namespace,
            graph_task_id=payload.id,
            node_name=payload.name,
        )
        existing_scope = staged_graph_scopes.get(child_namespace)
        if existing_scope is not None and existing_scope != graph_scope:
            raise ValueError(
                f"conflicting task start for namespace={child_namespace!r}"
            )
        staged_graph_scopes[child_namespace] = graph_scope
        if payload.name == "tools":
            payload_tool_ids: set[str] = set()
            for tool_call in tool_calls:
                scoped_id = self._tool_call_id(parent_namespace, tool_call.id)
                if scoped_id in payload_tool_ids:
                    if tool_call.name == "task":
                        raise ValueError(
                            f"duplicate parent tool call ID: {tool_call.id}"
                        )
                    raise ValueError(f"duplicate native tool call ID: {tool_call.id}")
                payload_tool_ids.add(scoped_id)
            if existing_fingerprint is None:
                existing_native_ids = {
                    self._tool_call_id(namespace, call.id)
                    for (namespace, _), calls in self._native_tool_calls.items()
                    for call in calls
                }
                for tool_call in tool_calls:
                    scoped_id = self._tool_call_id(parent_namespace, tool_call.id)
                    if scoped_id in existing_native_ids:
                        raise ValueError(
                            f"duplicate native tool call ID: {tool_call.id}"
                        )
            for tool_call in tool_calls:
                scoped_id = self._tool_call_id(parent_namespace, tool_call.id)
                known_name = staged_tool_names.get(scoped_id)
                if known_name is not None and known_name != tool_call.name:
                    raise ValueError(f"duplicate native tool call ID: {tool_call.id}")
                staged_tool_names[scoped_id] = tool_call.name
            for tool_call in tool_calls:
                calls = staged_native_tool_calls.setdefault(
                    (parent_namespace, tool_call.name), []
                )
                if all(existing.id != tool_call.id for existing in calls):
                    calls.append(tool_call)
            task_calls = [call for call in tool_calls if call.name == "task"]
            parsed_calls = [
                (task_call, SubagentTaskInput.model_validate(task_call.args.root))
                for task_call in task_calls
            ]
            multiple = len(parsed_calls) > 1
            for index, (task_call, descriptor) in enumerate(parsed_calls):
                suffix = f"{payload.id}:{index}" if multiple else payload.id
                child_namespace = (*parent_namespace, f"tools:{suffix}")
                graph_scope = GraphScope(
                    namespace=child_namespace,
                    parent_namespace=parent_namespace,
                    graph_task_id=payload.id,
                    node_name=payload.name,
                )
                existing_scope = staged_graph_scopes.get(child_namespace)
                if existing_scope is not None and existing_scope != graph_scope:
                    raise ValueError(
                        f"conflicting task start for namespace={child_namespace!r}"
                    )
                staged_graph_scopes[child_namespace] = graph_scope
                parent_tool_call_id = self._tool_call_id(
                    parent_namespace,
                    task_call.id,
                )
                invocation = SubagentInvocation(
                    parent_namespace=parent_namespace,
                    child_namespace=child_namespace,
                    graph_task_id=payload.id,
                    parent_tool_call_id=parent_tool_call_id,
                    subagent_input=descriptor.description,
                    agent_name=descriptor.subagent_type,
                )
                existing = staged_subagent_invocations.get(child_namespace)
                if existing is not None:
                    if existing != invocation:
                        raise ValueError(
                            f"conflicting task start for namespace={child_namespace!r}"
                        )
                else:
                    parent_key = (parent_namespace, task_call.id)
                    related = staged_sub_namespaces.get(parent_key)
                    if related is not None and related != child_namespace:
                        raise ValueError(
                            f"duplicate parent tool call ID: {task_call.id}"
                        )
                    staged_subagent_invocations[child_namespace] = invocation
                    staged_sub_namespaces[parent_key] = child_namespace
                    staged_agent_names[child_namespace] = descriptor.subagent_type
                subagents.append(
                    {
                        "namespace": list(child_namespace),
                        "graphTaskId": payload.id,
                        "agentName": descriptor.subagent_type,
                        "parentToolCallId": parent_tool_call_id,
                        "description": descriptor.description,
                    }
                )
        event = self._task_raw_event(
            namespace=parent_namespace,
            source=parent_source,
            phase="start",
            data={
                "id": payload.id,
                "name": payload.name,
                "input": payload.input,
                "triggers": payload.triggers,
                **(
                    {
                        "metadata": payload.metadata.model_dump(
                            mode="python",
                            by_alias=False,
                            exclude_none=True,
                            exclude_defaults=True,
                        )
                    }
                    if payload.metadata is not None
                    else {}
                ),
            },
            subagents=subagents,
        )
        staged_task_start_fingerprints = dict(self._task_start_fingerprints)
        staged_task_start_fingerprints[fingerprint_key] = fingerprint
        (
            self._native_tool_calls,
            self._graph_scopes,
            self._subagent_invocations,
            self._sub_namespaces_by_parent_tool_call,
            self._namespace_agent_names,
            self._tool_names_by_id,
            self._task_start_fingerprints,
        ) = (
            staged_native_tool_calls,
            staged_graph_scopes,
            staged_subagent_invocations,
            staged_sub_namespaces,
            staged_agent_names,
            staged_tool_names,
            staged_task_start_fingerprints,
        )
        return [event]

    def _process_task_result(
        self,
        namespace: tuple[str, ...],
        payload: TaskResultPayload,
    ) -> list[BaseEvent]:
        source = self._source(namespace)
        self._require_started_source(source)
        key = (namespace, payload.id)
        start = self._task_start_fingerprints.get(key)
        if start is None:
            raise ValueError(
                "task result has no matching task start: "
                f"namespace={namespace!r} graph_task_id={payload.id!r}"
            )
        if start.name != payload.name:
            raise ValueError(
                "conflicting task result for "
                f"namespace={namespace!r} graph_task_id={payload.id!r}"
            )
        public_error = _public_task_error(payload.error)
        fingerprint = TaskResultFingerprint(
            name=payload.name,
            error_json=json.dumps(
                _to_json_value(public_error),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            interrupts_json=json.dumps(
                _to_json_value(payload.interrupts),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            result_json=json.dumps(
                _to_json_value(payload.result),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        previous = self._task_result_fingerprints.get(key)
        if previous is not None:
            if previous == fingerprint:
                return []
            raise ValueError(
                "conflicting task result for "
                f"namespace={namespace!r} graph_task_id={payload.id!r}"
            )
        event = self._task_raw_event(
            namespace=namespace,
            source=source,
            phase="result",
            data={
                "id": payload.id,
                "name": payload.name,
                "error": public_error,
                "interrupts": payload.interrupts,
                "result": payload.result,
            },
        )
        self._task_result_fingerprints[key] = fingerprint
        return [event]

    def _task_raw_event(
        self,
        *,
        namespace: tuple[str, ...],
        source: AgentSource,
        phase: Literal["start", "result"],
        data: Mapping[str, object],
        subagents: Sequence[dict[str, JsonValue]] = (),
    ) -> RawEvent:
        """Build a task RAW event after JSON and provider-privacy filtering."""

        provenance = source.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        if subagents:
            provenance["subagents"] = list(subagents)
        raw_data = dict(data)
        operational_input: JsonValue | None = None
        if phase == "start" and raw_data.get("name") == "tools":
            operational_input = normalize_operational_data(raw_data.pop("input"))
        public_event = sanitize_public_data(
            {"data": raw_data, "provenance": provenance}
        )
        if not isinstance(public_event, dict):
            raise TypeError("task RAW event must be a JSON object")
        public_data = public_event.get("data")
        if operational_input is not None:
            if not isinstance(public_data, dict):
                raise TypeError("task RAW data must be a JSON object")
            public_data["input"] = operational_input
        return RawEvent(
            source="langgraph.tasks",
            raw_event={"type": "tasks", "phase": phase, "ns": list(namespace)},
            event=public_event,
        )

    def _process_values_part(self, part: ValuesStreamPart) -> list[BaseEvent]:
        source = self._source(part.ns)
        self._require_started_source(source)
        return self._emit_values_part(part)

    def _process_extra_part(self, part: ExtraStreamPart) -> list[BaseEvent]:
        """Project an additional native mode without exposing runtime config."""

        source = self._source(part.ns)
        self._require_started_source(source)
        data = (
            _safe_checkpoint_payload(part.data)
            if part.type in {"checkpoints", "debug"}
            else sanitize_public_data(part.data)
        )
        public_event = sanitize_public_data(
            {
                "data": data,
                "provenance": source.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
            }
        )
        if not isinstance(public_event, dict):
            raise TypeError("RAW stream event must be a JSON object")
        return [
            RawEvent(
                source=f"langgraph.{part.type}",
                raw_event={"type": part.type, "ns": list(part.ns)},
                event=public_event,
            )
        ]

    def _emit_values_part(self, part: ValuesStreamPart) -> list[BaseEvent]:
        source = self._source(part.ns)
        raw_event = self._event_context("values", source)
        raw_messages = part.data.get(_MESSAGE_STATE_KEY, [])
        # Messages have a dedicated AG-UI channel. Keeping LangChain messages in
        # `STATE_*` would duplicate data, so state events contain non-message state.
        raw_current = {
            key: value for key, value in part.data.items() if key != _MESSAGE_STATE_KEY
        }
        current_value = sanitize_public_data(raw_current)
        if not isinstance(current_value, dict):
            raise TypeError("values state without messages must be a JSON object")
        current = current_value
        events: list[BaseEvent] = []
        if source.kind != "root":
            event = RawEvent(
                source="langgraph.values",
                raw_event={"type": "values", "ns": list(part.ns)},
                event={
                    "state": current,
                    "provenance": source.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    ),
                },
            )
            if part.interrupts:
                self._buffer_child_interrupts(part, source, raw_messages)
            return [event]

        prepared_interrupts, propagated_child_ids, child_namespaces = (
            self._prepare_root_interrupts(part.interrupts, source, raw_messages)
        )
        converted_messages = (
            self._merge_message_snapshots(
                self._convert_messages(raw_messages, part.ns),
                child_namespaces,
            )
            if prepared_interrupts
            else None
        )

        previous = self._previous_root_state
        # Root state is an authoritative synchronization boundary. Providers may
        # omit a final chunk marker, so close every child channel before publishing
        # either a snapshot or a delta.
        events.extend(self._close_all_reasoning())
        events.extend(self._close_all_messages())
        events.extend(self._close_all_tools())
        if prepared_interrupts:
            events.append(StateSnapshotEvent(snapshot=current, raw_event=raw_event))
            events.append(
                MessagesSnapshotEvent(
                    messages=converted_messages or [],
                    raw_event=raw_event,
                )
            )
            self._previous_root_state = current
            self._record_interrupts(part.ns, prepared_interrupts)
            self._resolved_child_interrupt_ids.update(propagated_child_ids)
            return events

        if previous is None and not current:
            # A messages-only values frame has no visible state after filtering, so it
            # cannot establish a patch baseline the client never received
            return events
        if previous is None:
            events.append(StateSnapshotEvent(snapshot=current, raw_event=raw_event))
        else:
            operations = _json_patch(previous, current)
            if operations:
                events.append(
                    StateDeltaEvent(
                        delta=[
                            operation.model_dump(
                                mode="json",
                                by_alias=True,
                                exclude_unset=True,
                            )
                            for operation in operations
                        ],
                        raw_event=raw_event,
                    )
                )
        self._previous_root_state = current

        return events

    @staticmethod
    def _interrupt_value_json(interrupt: AgentRuntimeInterrupt) -> str:
        """Return a strict finite fingerprint for propagation comparisons."""

        return json.dumps(
            _to_json_value(interrupt.value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
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
        self,
        part: ValuesStreamPart,
        source: AgentSource,
        raw_messages: object,
    ) -> None:
        """Validate and stage child interrupts until root propagation arrives."""

        native_by_id: dict[str, AgentRuntimeInterrupt] = {}
        new_interrupts: list[AgentRuntimeInterrupt] = []
        for native in part.interrupts:
            if native.id in native_by_id:
                raise InterruptCorrelationError(
                    f"duplicate child interrupt ID: {native.id}"
                )
            native_by_id[native.id] = native
            existing = self._child_interrupts.get(native.id)
            if existing is None:
                new_interrupts.append(native)
                continue
            if existing.value_json != self._interrupt_value_json(native):
                raise InterruptCorrelationError(
                    f"conflicting child interrupt propagation for ID: {native.id}"
                )
            if part.ns not in existing.namespaces and any(
                not (
                    namespace == part.ns[: len(namespace)]
                    or part.ns == namespace[: len(part.ns)]
                )
                for namespace in existing.namespaces
            ):
                raise InterruptCorrelationError(
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
            raise InterruptCorrelationError(
                f"conflicting child interrupt set for namespace: {part.ns!r}"
            )
        previous_messages = self._child_message_snapshots.get(part.ns)
        if previous_ids is not None and previous_messages != converted_messages:
            raise InterruptCorrelationError(
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
        self,
        native_interrupts: Sequence[AgentRuntimeInterrupt],
        source: AgentSource,
        raw_messages: object,
    ) -> tuple[list[AgUiInterrupt], set[str], tuple[tuple[str, ...], ...]]:
        """Match root interrupts to buffered children and prepare root-local ones."""

        native_by_id: dict[str, AgentRuntimeInterrupt] = {}
        for native in native_interrupts:
            if native.id in native_by_id:
                raise InterruptCorrelationError(
                    f"duplicate root interrupt ID: {native.id}"
                )
            native_by_id[native.id] = native
            buffered = self._child_interrupts.get(native.id)
            if (
                buffered is not None
                and buffered.value_json != self._interrupt_value_json(native)
            ):
                raise InterruptCorrelationError(
                    f"conflicting root propagation for child interrupt ID: {native.id}"
                )

        unresolved = set(self._child_interrupts) - self._resolved_child_interrupt_ids
        missing = unresolved - set(native_by_id)
        if missing:
            raise InterruptCorrelationError(
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

    def _merge_message_snapshots(
        self,
        root_messages: Sequence[Message],
        child_namespaces: Sequence[tuple[str, ...]],
    ) -> list[Message]:
        """Merge root-first scoped snapshots and reject conflicting duplicate IDs."""

        merged: list[Message] = []
        fingerprints: dict[str, str] = {}
        groups = [
            tuple(root_messages),
            *(
                self._child_message_snapshots.get(namespace, ())
                for namespace in child_namespaces
            ),
        ]
        for messages in groups:
            for message in messages:
                fingerprint = json.dumps(
                    _to_json_value(
                        message.model_dump(
                            mode="python",
                            by_alias=True,
                            exclude_none=False,
                        )
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                previous = fingerprints.get(message.id)
                if previous is not None:
                    if previous != fingerprint:
                        raise ValueError(
                            f"conflicting snapshot message ID: {message.id}"
                        )
                    continue
                fingerprints[message.id] = fingerprint
                merged.append(message)
        return merged

    def _process_ai_chunk(
        self,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        visible_content = _visible_ai_content(chunk)
        reasoning_events = self._convert_reasoning_events(
            chunk,
            source,
            raw_event,
        )
        # An empty final frame is a provider-declared hard boundary. Only an empty
        # non-final frame is a heartbeat that leaves active lifecycles unchanged.
        if not visible_content and not chunk.tool_call_chunks and not reasoning_events:
            if chunk.chunk_position == "last":
                events.extend(self._close_tools(source.namespace, raw_event))
                events.extend(self._close_reasoning(source.namespace, raw_event))
                events.extend(self._close_message(source.namespace, raw_event))
            return events

        events.extend(reasoning_events)

        text = visible_content
        if text:
            # Visible answer text closes reasoning for this message before text starts.
            events.extend(self._close_reasoning(source.namespace, raw_event))
            raw_message_id = self._stable_message_id(chunk)
            message_id = self._message_id(source.namespace, raw_message_id)
            active_message_id = self._active_messages.get(source.namespace)
            if active_message_id != message_id:
                if active_message_id is not None:
                    events.append(
                        TextMessageEndEvent(
                            message_id=active_message_id,
                            raw_event=raw_event,
                        )
                    )
                self._active_messages[source.namespace] = message_id
                events.append(
                    TextMessageStartEvent(
                        message_id=message_id,
                        role="assistant",
                        name=source.agent_name,
                        raw_event=raw_event,
                    )
                )
            events.append(
                TextMessageContentEvent(
                    message_id=message_id,
                    delta=text,
                    raw_event=raw_event,
                )
            )

        if chunk.tool_call_chunks:
            events.extend(self._close_reasoning(source.namespace, raw_event))
            events.extend(self._close_message(source.namespace, raw_event))
            for tool_chunk in chunk.tool_call_chunks:
                events.extend(
                    self._process_tool_chunk(
                        tool_chunk,
                        chunk,
                        source,
                        raw_event,
                    )
                )
            if chunk.chunk_position == "last":
                events.extend(self._close_tools(source.namespace, raw_event))
        # Heartbeats were filtered above, so a Tool-free chunk here carries text or
        # reasoning. Only a final chunk closes all remaining lifecycles.
        elif chunk.chunk_position == "last":
            events.extend(self._close_tools(source.namespace, raw_event))
            events.extend(self._close_reasoning(source.namespace, raw_event))
            events.extend(self._close_message(source.namespace, raw_event))
        return events

    def _process_tool_chunk(
        self,
        tool_chunk: ToolCallChunk,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        index = tool_chunk.get("index")
        raw_source_message_id = self._stable_message_id(chunk)
        source_message_id = self._message_id(
            source.namespace,
            raw_source_message_id,
        )
        index_key = (source.namespace, source_message_id, index)
        chunk_id = tool_chunk.get("id")
        tool_call_id = (
            self._tool_call_id(source.namespace, str(chunk_id))
            if chunk_id
            else self._tool_ids_by_index.get(index_key)
        )
        name_value = tool_chunk.get("name")
        tool_name = name_value if name_value else None
        events: list[BaseEvent] = []

        if tool_call_id is None:
            raise RuntimeError("validated tool fragment lost its scoped start")

        if index_key in self._tool_ids_by_index:
            previous_id = self._tool_ids_by_index[index_key]
            if previous_id and previous_id != tool_call_id:
                events.extend(self._close_tool(str(previous_id), raw_event))

        active = self._active_tools.get(tool_call_id)
        if active is None and tool_call_id not in self._ended_tool_ids:
            if not tool_name:
                return events
            active = ActiveToolCall(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                parent_message_id=source_message_id,
                namespace=source.namespace,
                index=index,
            )
            self._active_tools[tool_call_id] = active
            self._tool_names_by_id[tool_call_id] = tool_name
            self._tool_history.setdefault((source.namespace, tool_name), []).append(
                active
            )
            self._tool_ids_by_index[index_key] = tool_call_id
            self._tool_id_history_by_index[index_key] = tool_call_id
            self._started_tool_ids.add(tool_call_id)
            events.append(
                ToolCallStartEvent(
                    tool_call_id=tool_call_id,
                    tool_call_name=tool_name,
                    parent_message_id=active.parent_message_id,
                    raw_event=raw_event,
                )
            )

        args_value = tool_chunk.get("args")
        if active is not None and isinstance(args_value, str) and args_value:
            active.arguments += args_value
            events.append(
                ToolCallArgsEvent(
                    tool_call_id=tool_call_id,
                    delta=args_value,
                    raw_event=raw_event,
                )
            )
        return events

    def _process_tool_result(
        self,
        message: ToolMessage,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        tool_call_id = self._tool_call_id(
            source.namespace,
            str(message.tool_call_id),
        )
        message_id = self._message_id(
            source.namespace,
            str(message.id or f"tool-result:{message.tool_call_id}"),
        )
        normalized_content = _normalize_tool_content(
            normalize_operational_data(message.content)
        )
        known_tool_name = self._tool_names_by_id.get(tool_call_id)
        if (
            message.name is not None
            and known_tool_name is not None
            and known_tool_name != message.name
        ):
            raise ValueError(
                "ToolMessage name does not match its scoped tool call ID: "
                f"{tool_call_id}"
            )
        effective_tool_name = known_tool_name or message.name or "tool"
        fingerprint = ToolResultFingerprint(
            message_id=message_id,
            tool_name=effective_tool_name,
            content=normalized_content,
            status=message.status,
        )
        previous = self._result_fingerprints.get(tool_call_id)
        if previous is not None:
            if previous == fingerprint:
                return []
            raise ValueError(
                f"conflicting ToolMessage for scoped tool call ID: {tool_call_id}"
            )
        events: list[BaseEvent] = []
        # A Tool result is a hard message boundary, including for providers that omit
        # the `last` marker on their final AI chunk.
        events.extend(self._close_reasoning(source.namespace, raw_event))
        events.extend(self._close_message(source.namespace, raw_event))
        if tool_call_id not in self._started_tool_ids:
            self._tool_names_by_id[tool_call_id] = effective_tool_name
            events.append(
                ToolCallStartEvent(
                    tool_call_id=tool_call_id,
                    tool_call_name=effective_tool_name,
                    # A result-only replay cannot recover the proposing assistant message.
                    parent_message_id=None,
                    raw_event=raw_event,
                )
            )
            self._started_tool_ids.add(tool_call_id)
        if tool_call_id not in self._ended_tool_ids and (
            tool_call_id in self._active_tools or tool_call_id in self._started_tool_ids
        ):
            events.extend(self._close_tool(tool_call_id, raw_event))
            if tool_call_id not in self._ended_tool_ids:
                self._ended_tool_ids.add(tool_call_id)
                events.append(
                    ToolCallEndEvent(
                        tool_call_id=tool_call_id,
                        raw_event=raw_event,
                    )
                )
        self._result_fingerprints[tool_call_id] = fingerprint
        self._prior_tool_call_ids.discard(tool_call_id)
        events.append(
            ToolCallResultEvent(
                message_id=message_id,
                tool_call_id=tool_call_id,
                content=normalized_content,
                role="tool",
                raw_event=raw_event,
            )
        )
        return events

    def _convert_reasoning_events(
        self,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        """Convert model reasoning events only when the client enables them."""

        if not self._expose_reasoning_events:
            return []

        events: list[BaseEvent] = []
        for delta in self._reasoning_deltas(chunk):
            events.extend(self._emit_reasoning(delta, chunk, source, raw_event))
        return events

    @staticmethod
    def _reasoning_deltas(chunk: AIMessageChunk) -> list[str]:
        """Read reasoning deltas only from the locked provider metadata path."""
        reasoning = chunk.additional_kwargs.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            return [reasoning]
        return []

    def _emit_reasoning(
        self,
        delta: str,
        chunk: AIMessageChunk,
        source: AgentSource,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        raw_source_message_id = self._stable_message_id(chunk)
        source_message_id = self._message_id(
            source.namespace,
            raw_source_message_id,
        )
        run_id = self._run_id_for(source)
        key = (run_id, source_message_id)
        events = self._close_message(source.namespace, raw_event)
        active = self._active_reasoning.get(key)
        if active is None:
            # Model calls are sequential within a run, so a new source message closes
            # the previous reasoning stream.
            events.extend(
                self._close_reasoning(
                    source.namespace,
                    raw_event,
                )
            )
            active = ActiveReasoning(
                run_id=run_id,
                source_message_id=source_message_id,
                reasoning_id=self._ids.encode(
                    "reasoning",
                    source.namespace,
                    raw_source_message_id,
                ),
                message_id=self._ids.encode(
                    "reasoning-message",
                    source.namespace,
                    raw_source_message_id,
                ),
                namespace=source.namespace,
            )
            self._active_reasoning[key] = active
            events.append(
                ReasoningStartEvent(
                    message_id=active.reasoning_id,
                    raw_event=raw_event,
                )
            )
            events.append(
                ReasoningMessageStartEvent(
                    message_id=active.message_id,
                    role="reasoning",
                    raw_event=raw_event,
                )
            )
        events.append(
            ReasoningMessageContentEvent(
                message_id=active.message_id,
                delta=delta,
                raw_event=raw_event,
            )
        )
        return events

    def _close_reasoning(
        self,
        namespace: tuple[str, ...],
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        for key, active in list(self._active_reasoning.items()):
            if active.namespace != namespace:
                continue
            self._active_reasoning.pop(key)
            events.extend(
                [
                    ReasoningMessageEndEvent(
                        message_id=active.message_id,
                        raw_event=raw_event,
                    ),
                    ReasoningEndEvent(
                        message_id=active.reasoning_id,
                        raw_event=raw_event,
                    ),
                ]
            )
        return events

    def _close_message(
        self,
        namespace: tuple[str, ...],
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        message_id = self._active_messages.pop(namespace, None)
        if message_id is None:
            return []
        return [TextMessageEndEvent(message_id=message_id, raw_event=raw_event)]

    def _close_tool(
        self,
        tool_call_id: str,
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        active = self._active_tools.pop(tool_call_id, None)
        if active is None or tool_call_id in self._ended_tool_ids:
            return []
        if active.parent_message_id is not None:
            self._tool_ids_by_index.pop(
                (active.namespace, active.parent_message_id, active.index),
                None,
            )
        self._ended_tool_ids.add(tool_call_id)
        return [ToolCallEndEvent(tool_call_id=tool_call_id, raw_event=raw_event)]

    def _close_tools(
        self,
        namespace: tuple[str, ...],
        raw_event: dict[str, JsonValue],
    ) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        for tool_call_id, active in list(self._active_tools.items()):
            if active.namespace == namespace:
                events.extend(self._close_tool(tool_call_id, raw_event))
        return events

    def _close_all_reasoning(self) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        namespaces = list(
            dict.fromkeys(
                active.namespace for active in self._active_reasoning.values()
            )
        )
        for namespace in namespaces:
            source = self._source(namespace)
            events.extend(
                self._close_reasoning(
                    namespace,
                    self._event_context("messages", source),
                )
            )
        return events

    def _close_all_messages(self) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        for namespace in list(self._active_messages):
            source = self._source(namespace)
            events.extend(
                self._close_message(
                    namespace,
                    self._event_context("messages", source),
                )
            )
        return events

    def _close_all_tools(self) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        for tool_call_id, active in list(self._active_tools.items()):
            source = self._source(active.namespace)
            events.extend(
                self._close_tool(
                    tool_call_id,
                    self._event_context("messages", source),
                )
            )
        return events

    def _record_agent_name(
        self,
        namespace: tuple[str, ...],
        agent_name: str | None,
    ) -> None:
        invocation = self._subagent_invocations.get(namespace)
        if invocation is None or not agent_name:
            return
        if invocation.agent_name != agent_name:
            raise ValueError(
                "task subagent_type does not match streamed lc_agent_name: "
                f"namespace={namespace!r} expected={invocation.agent_name!r} "
                f"actual={agent_name!r}"
            )
        self._namespace_agent_names[namespace] = agent_name

    def _require_started_source(self, source: AgentSource) -> None:
        if source.kind == "root":
            return
        if source.namespace not in self._graph_scopes:
            raise RuntimeError(
                "subgraph stream arrived before its native task-start correlation: "
                f"namespace={source.namespace!r}"
            )

    def _source(
        self,
        namespace: tuple[str, ...],
    ) -> AgentSource:
        if not namespace:
            return AgentSource(
                kind="root",
                agent_type="main",
                agent_name="main",
                namespace=namespace,
            )
        invocation = self._subagent_invocations.get(namespace)
        if invocation is not None:
            return AgentSource(
                kind="deep_agent_subagent",
                namespace=namespace,
                parent_namespace=invocation.parent_namespace,
                graph_task_id=invocation.graph_task_id,
                node_name="tools",
                agent_type="subagent",
                agent_name=self._namespace_agent_names.get(
                    namespace, invocation.agent_name
                ),
                parent_tool_call_id=invocation.parent_tool_call_id,
                subagent_input=invocation.subagent_input,
            )
        scope = self._graph_scopes.get(namespace)
        if scope is None:
            return AgentSource(kind="compiled_subgraph", namespace=namespace)
        return AgentSource(
            kind="compiled_subgraph",
            namespace=namespace,
            parent_namespace=scope.parent_namespace,
            graph_task_id=scope.graph_task_id,
            node_name=scope.node_name,
        )

    def _run_id_for(self, source: AgentSource) -> str:
        """Return the caller-declared main AG-UI run ID for every graph source."""

        return self.run_id

    def _event_context(
        self,
        stream_mode: StreamMode,
        source: AgentSource,
        *,
        langgraph_node: str | None = None,
        interrupt_id: str | None = None,
        related_namespace: tuple[str, ...] | None = None,
        parent_tool_call_id: str | None = None,
        tool_result_status: Literal["success", "error"] | None = None,
    ) -> dict[str, JsonValue]:
        context = EventContext(
            stream_mode=stream_mode,
            source=source,
            run_id=self._run_id_for(source),
            related_namespace=related_namespace,
            parent_tool_call_id=parent_tool_call_id,
            langgraph_node=langgraph_node,
            interrupt_id=interrupt_id,
            tool_result_status=tool_result_status,
        )
        return cast(
            dict[str, JsonValue],
            context.model_dump(mode="json", by_alias=True, exclude_none=True),
        )

    @staticmethod
    def _stable_message_id(message: AIMessage) -> str:
        """Require the framework-provided stable ID used for cross-frame correlation."""

        if not isinstance(message.id, str) or not message.id:
            raise ValueError("AI messages require a stable ID")
        return message.id

    def _message_id(
        self,
        namespace: tuple[str, ...],
        raw_id: str,
    ) -> str:
        """Encode a native message ID as a namespace-aware AG-UI ID."""

        return self._ids.encode("message", namespace, raw_id)

    def _tool_call_id(
        self,
        namespace: tuple[str, ...],
        raw_id: str,
    ) -> str:
        """Encode a native Tool call ID as a namespace-aware AG-UI ID."""

        return self._ids.encode("tool", namespace, raw_id)

    def _convert_messages(
        self,
        raw_messages: object,
        namespace: tuple[str, ...],
    ) -> list[Message]:
        """Project complete checkpoint messages into an authoritative AG-UI snapshot."""

        if not isinstance(raw_messages, Sequence) or isinstance(
            raw_messages,
            (str, bytes),
        ):
            raise TypeError("values messages must be a sequence")
        converted: list[Message] = []
        for index, message in enumerate(raw_messages):
            if not isinstance(message, BaseMessage):
                raise TypeError("values messages must contain LangChain messages")
            raw_message_id = (
                self._stable_message_id(message)
                if isinstance(message, AIMessage)
                else str(message.id or f"state-message-{index}")
            )
            message_id = self._message_id(namespace, raw_message_id)
            if isinstance(message, HumanMessage):
                converted.append(
                    UserMessage(
                        id=message_id,
                        content=_normalize_tool_content(message.content),
                        name=message.name,
                    )
                )
                continue
            if isinstance(message, SystemMessage):
                converted.append(
                    AgUiSystemMessage(
                        id=message_id,
                        content=_normalize_tool_content(message.content),
                        name=message.name,
                    )
                )
                continue
            if isinstance(message, ToolMessage):
                converted.append(
                    AgUiToolMessage(
                        id=message_id,
                        content=_normalize_tool_content(message.content),
                        tool_call_id=self._tool_call_id(
                            namespace,
                            str(message.tool_call_id),
                        ),
                    )
                )
                continue
            if isinstance(message, AIMessage):
                tool_calls: list[ToolCall] = []
                for call in message.tool_calls:
                    raw_call_id = call.get("id")
                    name = call.get("name")
                    if not raw_call_id or not isinstance(name, str) or not name:
                        raise ValueError(
                            "assistant snapshot tool calls require stable IDs and names"
                        )
                    arguments = json.dumps(
                        _to_json_value(call.get("args", {})),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    tool_calls.append(
                        ToolCall(
                            id=self._tool_call_id(namespace, str(raw_call_id)),
                            function=FunctionCall(
                                name=name,
                                arguments=arguments,
                            ),
                        )
                    )
                public_metadata = sanitize_public_data(
                    {
                        "additional_kwargs": message.additional_kwargs,
                        "response_metadata": message.response_metadata,
                    }
                )
                if not isinstance(public_metadata, dict):
                    raise TypeError("assistant metadata must be a JSON object")
                converted.append(
                    AssistantMessage.model_validate(
                        {
                            "id": message_id,
                            "content": _visible_ai_content(message),
                            "name": message.name,
                            "toolCalls": tool_calls or None,
                            **public_metadata,
                        }
                    )
                )
                continue
            raise ValueError(
                f"unsupported checkpoint message type: {type(message).__name__}"
            )
        return converted

    def _record_interrupts(
        self,
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
        self,
        native_interrupts: Sequence[AgentRuntimeInterrupt],
        source: AgentSource,
        raw_messages: object,
    ) -> list[AgUiInterrupt]:
        """Validate all HITL Tool correlations before mutating adapter state."""

        if not native_interrupts:
            return []
        parsed: list[tuple[AgentRuntimeInterrupt, HitlRequest | None]] = []
        action_groups: list[Sequence[HitlActionRequest]] = []
        for interrupt in native_interrupts:
            try:
                request = HitlRequest.model_validate(interrupt.value)
            except ValidationError as error:
                value = interrupt.value
                if isinstance(value, dict) and (
                    "action_requests" in value or "review_configs" in value
                ):
                    raise InterruptCorrelationError(
                        f"invalid Deep Agents HITL interrupt: {interrupt.id}"
                    ) from error
                request = None
            parsed.append((interrupt, request))
            if request is not None:
                action_groups.append(request.action_requests)

        matched_id_groups = self._tool_call_id_groups_for_actions(
            source.namespace,
            action_groups,
            raw_messages,
        )
        matched_group_index = 0
        prepared: list[AgUiInterrupt] = []
        for interrupt, request in parsed:
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
                raise InterruptCorrelationError(
                    f"invalid Deep Agents HITL interrupt: {interrupt.id}"
                )
            for public_action, action in zip(
                public_actions,
                request.action_requests,
                strict=True,
            ):
                if not isinstance(public_action, dict):
                    raise InterruptCorrelationError(
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
        self,
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
        self,
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
            checkpoint_messages = tuple(
                message for message in raw_messages if isinstance(message, BaseMessage)
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

    @staticmethod
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
