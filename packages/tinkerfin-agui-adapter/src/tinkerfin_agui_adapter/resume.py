"""Pure mapping from AG-UI resume entries to Deep Agents decision data."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Never, cast

from ag_ui.core.types import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .errors import (
    AgUiAdapterError,
    AgUiAdapterErrorCode,
    HitlCorrelationError,
)
from .hitl import (
    HitlActionRequest,
    HitlRequest,
    match_hitl_tool_call_id_groups,
)
from .ids import ScopedIdCodec
from .models import (
    AgentRuntimeInterrupt,
    JsonObject,
)
from .reasoning import json_values_equal, normalize_operational_data


class ResumeMappingError(AgUiAdapterError, ValueError):
    """Resume data cannot be translated without losing native semantics."""

    def __init__(
        self,
        code: AgUiAdapterErrorCode,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        if not code.name.startswith("RESUME_"):
            raise ValueError("code must identify an AG-UI resume failure")
        self.code = code
        super().__init__(message, cause=cause)


def _empty_decisions_by_interrupt() -> dict[
    str,
    tuple[dict[str, object] | None, ...],
]:
    """Create isolated decision storage for one translation."""

    return {}


@dataclass(frozen=True, slots=True)
class ResumeTranslation:
    """Lossless classification of resolved, abandoned, and mixed resume data.

    A `command` translation contains stock Deep Agents resume data; `abandon`
    contains only cancellations; `custom` preserves resolved decisions and
    cancelled slots without pretending stock Deep Agents can execute the mix.
    This value owns no graph, checkpointer, or I/O resource.
    """

    mode: Literal["command", "abandon", "custom"]
    resume_data: JsonObject | None
    cancelled_interrupt_ids: tuple[str, ...] = ()
    prior_tool_call_ids: tuple[str, ...] = ()
    decisions_by_interrupt: Mapping[
        str,
        tuple[dict[str, object] | None, ...],
    ] = field(default_factory=_empty_decisions_by_interrupt)

    @property
    def root(self) -> dict[str, JsonValue]:
        """Return stock Deep Agents resume data for a fully resolved translation."""

        if self.resume_data is None:
            raise RuntimeError("abandoned resume does not contain command data")
        return self.resume_data.root


class _PendingInterruptAction(BaseModel):
    """One review action extracted from a runtime interrupt."""

    model_config = ConfigDict(extra="forbid")

    ag_ui_interrupt_id: str = Field(min_length=1, description="AG-UI interrupt ID")
    interrupt_id: str = Field(min_length=1, description="runtime interrupt ID")
    action_index: int = Field(
        ge=0,
        description="Action position within the native interrupt batch",
    )
    action_name: str = Field(
        min_length=1,
        description="Name of the action under review",
    )
    allowed_decisions: list[Literal["approve", "edit", "reject", "respond"]] = Field(
        description="Decisions allowed for this action"
    )


class _PersistedDeepAgentCorrelation(BaseModel):
    """Validated resume correlation persisted inside an AG-UI interrupt."""

    model_config = ConfigDict(
        alias_generator=None,
        extra="allow",
        populate_by_name=True,
        strict=True,
    )

    tool_name: str = Field(
        alias="toolName",
        min_length=1,
        description="Reviewed Deep Agents Tool name",
    )
    allowed_decisions: list[Literal["approve", "edit", "reject", "respond"]] = Field(
        alias="allowedDecisions",
        min_length=1,
        description="Decisions allowed for the reviewed action",
    )
    original_args: JsonObject = Field(
        alias="originalArgs",
        description="Original reviewed Tool arguments",
    )
    native_interrupt_id: str | None = Field(
        default=None,
        alias="nativeInterruptId",
        min_length=1,
        description="Native LangGraph interrupt group ID",
    )
    action_index: int | None = Field(
        default=None,
        alias="actionIndex",
        ge=0,
        description="Action position within the native interrupt group",
    )


_PriorToolCallIdResolver = Callable[
    [Mapping[str, Sequence[bool]] | None],
    tuple[str, ...],
]


class ResumeMapper:
    """Translate complete AG-UI resume coverage without performing I/O.

    The mapper restores native interrupt grouping and action order, validates
    allowed decisions, and preserves `cancelled` as abandonment rather than a
    fabricated rejection. It neither queries a checkpointer nor constructs,
    invokes, or owns a graph command; the host decides how to execute the returned
    `ResumeTranslation`.
    """

    def map(
        self,
        *,
        entries: Sequence[ResumeEntry],
        interrupts: Sequence[AgentRuntimeInterrupt],
        messages_by_namespace: Mapping[tuple[str, ...], Sequence[BaseMessage]]
        | None = None,
    ) -> ResumeTranslation:
        """Classify AG-UI resume entries as command, abandonment, or custom data.

        Args:
            entries: Resume entries already validated by the AG-UI schema.
            interrupts: Pending interrupts supplied from a host checkpoint snapshot.
            messages_by_namespace: Complete checkpoint messages grouped by their
                full graph namespace. Required when at least one review is resolved;
                an all-cancelled abandonment does not need Tool-call correlation.

        Returns:
            A lossless translation preserving grouping and native action order.

        Raises:
            ResumeMappingError: Coverage is incomplete, IDs are invalid, checkpoint
                correlation evidence is missing or ambiguous, or a decision is not
                allowed.
        """

        pending, action_groups = self._pending_actions(interrupts)
        group_ids = tuple(
            dict.fromkeys(action.interrupt_id for action in pending.values())
        )

        def resolve_prior_tool_call_ids(
            selected: Mapping[str, Sequence[bool]] | None,
        ) -> tuple[str, ...]:
            selected_slots = (
                None
                if selected is None
                else tuple(tuple(selected[group_id]) for group_id in group_ids)
            )
            return self._prior_tool_call_ids(
                action_groups,
                messages_by_namespace,
                selected_slots=selected_slots,
            )

        return self._translate(
            entries=entries,
            pending=pending,
            resolve_prior_tool_call_ids=resolve_prior_tool_call_ids,
        )

    def map_agui(
        self,
        *,
        entries: Sequence[ResumeEntry],
        interrupts: Sequence[AgUiInterrupt],
    ) -> ResumeTranslation:
        """Translate resume entries from trusted, previously emitted interrupts.

        Args:
            entries: Resume entries already validated at the AG-UI request boundary.
            interrupts: Complete AG-UI interrupts persisted by the host from a prior
                run terminal. Client-supplied interrupt payloads are not trustworthy
                correlation evidence and must not be passed here.

        Returns:
            A lossless translation with Tool IDs already correlated at emission time.

        Raises:
            ResumeMappingError: Interrupt correlation is incomplete, inconsistent, or
                cannot be validated without weakening native resume semantics.
        """

        pending, tool_ids_by_group = self._pending_agui_actions(interrupts)

        def resolve_prior_tool_call_ids(
            selected: Mapping[str, Sequence[bool]] | None,
        ) -> tuple[str, ...]:
            resolved: list[str] = []
            for group_id, tool_ids in tool_ids_by_group.items():
                selected_group = (
                    tuple(True for _tool_id in tool_ids)
                    if selected is None
                    else tuple(selected[group_id])
                )
                resolved.extend(
                    tool_id
                    for tool_id, include in zip(
                        tool_ids,
                        selected_group,
                        strict=True,
                    )
                    if include
                )
            return tuple(resolved)

        return self._translate(
            entries=entries,
            pending=pending,
            resolve_prior_tool_call_ids=resolve_prior_tool_call_ids,
        )

    def _translate(
        self,
        *,
        entries: Sequence[ResumeEntry],
        pending: Mapping[str, _PendingInterruptAction],
        resolve_prior_tool_call_ids: _PriorToolCallIdResolver,
    ) -> ResumeTranslation:
        if not pending:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_NO_PENDING_INTERRUPT,
                "the thread has no pending review to resume",
            )

        expected_ids = set(pending)
        received_ids: set[str] = set()
        grouped_counts: dict[str, int] = {}
        grouped_decisions: dict[str, list[dict[str, object] | None]] = {}
        cancelled_interrupt_ids: list[str] = []
        for action in pending.values():
            grouped_counts[action.interrupt_id] = (
                grouped_counts.get(action.interrupt_id, 0) + 1
            )
        for interrupt_id, count in grouped_counts.items():
            grouped_decisions[interrupt_id] = [None] * count

        for entry in entries:
            interrupt_id = entry.interrupt_id
            if interrupt_id in received_ids:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_DUPLICATE_INTERRUPT_ID,
                    f"resume contains duplicate interruptId: {interrupt_id}",
                )
            if interrupt_id not in pending:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_UNKNOWN_INTERRUPT_ID,
                    f"resume contains unknown interruptId: {interrupt_id}",
                )
            received_ids.add(interrupt_id)
            pending_action = pending[interrupt_id]
            if entry.status == "cancelled":
                if entry.payload is not None:
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
                        f"cancelled interruptId={entry.interrupt_id} cannot include payload",
                    )
                cancelled_interrupt_ids.append(entry.interrupt_id)
            else:
                grouped_decisions[pending_action.interrupt_id][
                    pending_action.action_index
                ] = self._decision(entry, pending_action)

        missing_ids = sorted(expected_ids - received_ids)
        if missing_ids:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INCOMPLETE,
                "resume must cover every pending review; missing: "
                f"{', '.join(missing_ids)}",
            )

        if cancelled_interrupt_ids and len(cancelled_interrupt_ids) == len(pending):
            return ResumeTranslation(
                mode="abandon",
                resume_data=None,
                cancelled_interrupt_ids=tuple(cancelled_interrupt_ids),
                decisions_by_interrupt={
                    interrupt_id: tuple(decisions)
                    for interrupt_id, decisions in grouped_decisions.items()
                },
            )

        if cancelled_interrupt_ids:
            resolved_slots = {
                interrupt_id: tuple(decision is not None for decision in decisions)
                for interrupt_id, decisions in grouped_decisions.items()
            }
            prior_tool_call_ids = resolve_prior_tool_call_ids(
                resolved_slots,
            )
            return ResumeTranslation(
                mode="custom",
                resume_data=None,
                cancelled_interrupt_ids=tuple(cancelled_interrupt_ids),
                prior_tool_call_ids=prior_tool_call_ids,
                decisions_by_interrupt={
                    interrupt_id: tuple(decisions)
                    for interrupt_id, decisions in grouped_decisions.items()
                },
            )

        serialized_groups: dict[str, dict[str, object]] = {}
        for interrupt_id, decisions in grouped_decisions.items():
            if any(decision is None for decision in decisions):
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INCOMPLETE,
                    f"interruptId={interrupt_id} has incomplete review decisions",
                )
            serialized_groups[interrupt_id] = {
                "decisions": [
                    decision for decision in decisions if decision is not None
                ]
            }

        if len(serialized_groups) == 1:
            resume_data = JsonObject.model_validate(
                next(iter(serialized_groups.values()))
            )
        else:
            resume_data = JsonObject.model_validate(serialized_groups)
        prior_tool_call_ids = resolve_prior_tool_call_ids(None)
        return ResumeTranslation(
            mode="command",
            resume_data=resume_data,
            prior_tool_call_ids=prior_tool_call_ids,
            decisions_by_interrupt={
                interrupt_id: tuple(decisions)
                for interrupt_id, decisions in grouped_decisions.items()
            },
        )

    @classmethod
    def _pending_agui_actions(
        cls,
        interrupts: Sequence[AgUiInterrupt],
    ) -> tuple[
        dict[str, _PendingInterruptAction],
        dict[str, tuple[str, ...]],
    ]:
        pending: dict[str, _PendingInterruptAction] = {}
        groups: dict[
            str,
            tuple[HitlRequest, list[str | None]],
        ] = {}
        seen_tool_ids: set[str] = set()
        codec = ScopedIdCodec()

        for interrupt in interrupts:
            try:
                if interrupt.reason != "tool_call":
                    raise ValueError("interrupt reason is not tool_call")
                tool_call_id = interrupt.tool_call_id
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise ValueError("interrupt does not contain a Tool call ID")
                kind, _namespace, _raw_id = codec.decode(tool_call_id)
                if kind != "tool":
                    raise ValueError("interrupt ID does not identify a Tool call")
                if tool_call_id in seen_tool_ids:
                    raise ValueError("interrupts reuse a scoped Tool call ID")

                metadata = interrupt.metadata
                if not isinstance(metadata, Mapping):
                    raise TypeError("interrupt metadata must be an object")
                request = HitlRequest.model_validate(metadata.get("langgraphValue"))
                correlation = _PersistedDeepAgentCorrelation.model_validate(
                    metadata.get("deepagents")
                )
                native_id, action_index = cls._agui_action_position(
                    interrupt=interrupt,
                    request=request,
                    correlation=correlation,
                )
                action = request.action_requests[action_index]
                review = request.review_configs[action_index]
                if correlation.tool_name != action.name:
                    raise ValueError("persisted Tool name does not match native action")
                if correlation.allowed_decisions != review.allowed_decisions:
                    raise ValueError(
                        "persisted decisions do not match native review policy"
                    )
                if not json_values_equal(
                    correlation.original_args.root,
                    action.args.root,
                ):
                    raise ValueError(
                        "persisted Tool arguments do not match native action"
                    )
            except (TypeError, ValueError, ValidationError) as error:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                    f"persisted AG-UI interrupt is not resumable: {interrupt.id}",
                    cause=error,
                ) from error

            existing = groups.get(native_id)
            if existing is None:
                slots: list[str | None] = [None] * len(request.action_requests)
                groups[native_id] = (request, slots)
            else:
                existing_request, slots = existing
                if not json_values_equal(
                    existing_request.model_dump(mode="json", by_alias=True),
                    request.model_dump(mode="json", by_alias=True),
                ):
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                        f"persisted AG-UI interrupt group is inconsistent: {native_id}",
                    )
            if slots[action_index] is not None:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
                    f"duplicate pending interruptId: {interrupt.id}",
                )
            if interrupt.id in pending:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
                    f"duplicate pending interruptId: {interrupt.id}",
                )
            slots[action_index] = tool_call_id
            seen_tool_ids.add(tool_call_id)
            pending[interrupt.id] = _PendingInterruptAction(
                ag_ui_interrupt_id=interrupt.id,
                interrupt_id=native_id,
                action_index=action_index,
                action_name=action.name,
                allowed_decisions=review.allowed_decisions,
            )

        ordered_pending: dict[str, _PendingInterruptAction] = {}
        tool_ids_by_group: dict[str, tuple[str, ...]] = {}
        for native_id, (request, slots) in groups.items():
            if any(tool_id is None for tool_id in slots):
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INCOMPLETE,
                    f"persisted AG-UI interrupt group is incomplete: {native_id}",
                )
            multi_action = len(request.action_requests) > 1
            for index in range(len(slots)):
                public_id = f"{native_id}#{index}" if multi_action else native_id
                ordered_pending[public_id] = pending[public_id]
            tool_ids_by_group[native_id] = tuple(
                cast(str, tool_id) for tool_id in slots
            )
        return ordered_pending, tool_ids_by_group

    @staticmethod
    def _agui_action_position(
        *,
        interrupt: AgUiInterrupt,
        request: HitlRequest,
        correlation: _PersistedDeepAgentCorrelation,
    ) -> tuple[str, int]:
        native_id = correlation.native_interrupt_id
        action_index = correlation.action_index
        if (native_id is None) != (action_index is None):
            raise ValueError(
                "nativeInterruptId and actionIndex must be persisted together"
            )
        if native_id is None:
            if len(request.action_requests) == 1:
                native_id = interrupt.id
                action_index = 0
            else:
                native_id, separator, raw_index = interrupt.id.rpartition("#")
                if (
                    not separator
                    or not native_id
                    or not raw_index.isascii()
                    or not raw_index.isdecimal()
                ):
                    raise ValueError("multi-action interrupt ID has no action index")
                action_index = int(raw_index)
                if str(action_index) != raw_index:
                    raise ValueError("multi-action interrupt index is not canonical")
        assert action_index is not None
        expected_id = (
            native_id
            if len(request.action_requests) == 1
            else f"{native_id}#{action_index}"
        )
        if interrupt.id != expected_id or action_index >= len(request.action_requests):
            raise ValueError(
                "persisted interrupt ID does not match its action position"
            )
        return native_id, action_index

    @staticmethod
    def _prior_tool_call_ids(
        action_groups: Sequence[Sequence[HitlActionRequest]],
        messages_by_namespace: Mapping[tuple[str, ...], Sequence[BaseMessage]] | None,
        *,
        selected_slots: Sequence[Sequence[bool]] | None = None,
    ) -> tuple[str, ...]:
        """Correlate complete action groups, then return the selected Tool call IDs."""

        if messages_by_namespace is None:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_CHECKPOINT_MESSAGES_REQUIRED,
                "resolved Tool reviews require checkpoint messages grouped by "
                "their full graph namespace",
            )
        codec = ScopedIdCodec()
        scoped_messages: list[BaseMessage] = []
        try:
            for namespace, messages in messages_by_namespace.items():
                if not isinstance(namespace, tuple):
                    raise TypeError("message namespace must be a tuple")
                for message in messages:
                    if not isinstance(message, BaseMessage):
                        raise TypeError(
                            "checkpoint messages must contain LangChain messages"
                        )
                    if not isinstance(message, AIMessage):
                        scoped_messages.append(message)
                        continue
                    tool_calls = [
                        {
                            **call,
                            "id": codec.encode(
                                "tool",
                                namespace,
                                str(call.get("id") or ""),
                            ),
                        }
                        for call in message.tool_calls
                    ]
                    scoped_messages.append(
                        message.model_copy(update={"tool_calls": tool_calls})
                    )
        except (TypeError, ValueError) as error:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                "checkpoint messages do not have valid graph namespaces or Tool IDs",
                cause=error,
            ) from error
        try:
            matched_groups = match_hitl_tool_call_id_groups(
                action_groups,
                scoped_messages,
                selected_slots=selected_slots,
            )
        except HitlCorrelationError as error:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                "checkpoint review actions cannot be correlated to unique Tool calls",
                cause=error,
            ) from error
        return tuple(call_id for group in matched_groups for call_id in group)

    @staticmethod
    def _pending_actions(
        interrupts: Sequence[AgentRuntimeInterrupt],
    ) -> tuple[
        dict[str, _PendingInterruptAction],
        list[tuple[HitlActionRequest, ...]],
    ]:
        pending: dict[str, _PendingInterruptAction] = {}
        action_groups: list[tuple[HitlActionRequest, ...]] = []
        for interrupt in interrupts:
            try:
                request = HitlRequest.model_validate(interrupt.value)
            except ValidationError as exc:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_INTERRUPT_UNSUPPORTED,
                    "the thread contains an unsupported interrupt type",
                    cause=exc,
                ) from exc

            action_groups.append(tuple(request.action_requests))
            multi_action = len(request.action_requests) > 1
            for index, (action, review) in enumerate(
                zip(
                    request.action_requests,
                    request.review_configs,
                    strict=True,
                )
            ):
                ag_ui_interrupt_id = (
                    f"{interrupt.id}#{index}" if multi_action else interrupt.id
                )
                if ag_ui_interrupt_id in pending:
                    raise ResumeMappingError(
                        AgUiAdapterErrorCode.RESUME_DUPLICATE_PENDING_INTERRUPT_ID,
                        f"duplicate pending interruptId: {ag_ui_interrupt_id}",
                    )
                pending[ag_ui_interrupt_id] = _PendingInterruptAction(
                    ag_ui_interrupt_id=ag_ui_interrupt_id,
                    interrupt_id=interrupt.id,
                    action_index=index,
                    action_name=action.name,
                    allowed_decisions=review.allowed_decisions,
                )
        return pending, action_groups

    @staticmethod
    def _decision(
        entry: ResumeEntry,
        pending: _PendingInterruptAction,
    ) -> dict[str, object]:
        raw_payload = cast(object, entry.payload)
        if raw_payload is None:
            raise ResumeMappingError(
                AgUiAdapterErrorCode.RESUME_PAYLOAD_REQUIRED,
                f"interruptId={entry.interrupt_id} requires a payload",
            )
        if not isinstance(raw_payload, Mapping):
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        payload = dict(cast(Mapping[object, object], raw_payload))
        decision_type = payload.get("type")
        if decision_type not in {"approve", "edit", "reject", "respond"}:
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        decision = cast(
            Literal["approve", "edit", "reject", "respond"],
            decision_type,
        )
        ResumeMapper._require_decision(
            entry.interrupt_id,
            pending,
            decision,
        )

        if decision == "approve":
            if set(payload) != {"type"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            return {"type": "approve"}

        if decision == "edit":
            if set(payload) != {"type", "edited_action"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            edited_action = payload.get("edited_action")
            if not isinstance(edited_action, Mapping):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            edited_mapping = cast(Mapping[object, object], edited_action)
            if set(edited_mapping) != {"name", "args"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            name = edited_mapping.get("name")
            args = edited_mapping.get("args")
            if not isinstance(name, str) or not name or not isinstance(args, Mapping):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            args_mapping = cast(Mapping[object, object], args)
            # Deep Agents does not re-run review policy after an edit, so the Tool
            # identity is fixed and only its arguments may change.
            if name != pending.action_name:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            try:
                normalized = normalize_operational_data(args_mapping)
                normalized_args = JsonObject.model_validate(normalized).root
            except (TypeError, ValueError) as error:
                raise ResumeMappingError(
                    AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
                    f"interruptId={entry.interrupt_id} has an invalid payload",
                    cause=error,
                ) from error
            return {
                "type": "edit",
                "edited_action": {"name": name, "args": normalized_args},
            }

        if decision == "reject":
            if not set(payload) <= {"type", "message"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            if "message" not in payload:
                return {"type": "reject"}
            message = payload["message"]
            if not isinstance(message, str):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            return {"type": "reject", "message": message}

        if set(payload) != {"type", "message"}:
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        message = payload.get("message")
        if not isinstance(message, str):
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        return {"type": "respond", "message": message}

    @staticmethod
    def _raise_invalid_payload(interrupt_id: str) -> Never:
        raise ResumeMappingError(
            AgUiAdapterErrorCode.RESUME_PAYLOAD_INVALID,
            f"interruptId={interrupt_id} has an invalid payload",
        )

    @staticmethod
    def _require_decision(
        interrupt_id: str,
        pending: _PendingInterruptAction,
        decision: Literal["approve", "edit", "reject", "respond"],
    ) -> None:
        if decision in pending.allowed_decisions:
            return
        raise ResumeMappingError(
            AgUiAdapterErrorCode.RESUME_DECISION_NOT_ALLOWED,
            f"interruptId={interrupt_id} does not allow {decision}",
        )
