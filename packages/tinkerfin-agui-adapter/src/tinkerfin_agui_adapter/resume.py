"""Pure mapping from AG-UI resume entries to Deep Agents decision data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Never, cast

from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .hitl import (
    HitlActionRequest,
    HitlCorrelationError,
    HitlRequest,
    match_hitl_tool_call_id_groups,
)
from .ids import ScopedIdCodec
from .models import (
    AgentRuntimeInterrupt,
    JsonObject,
)
from .reasoning import normalize_operational_data


class ResumeMappingFailure(StrEnum):
    """Stable categories for failures at the resume-mapping boundary."""

    INTERRUPT_UNSUPPORTED = "interrupt_unsupported"
    DUPLICATE_PENDING_INTERRUPT_ID = "duplicate_pending_interrupt_id"
    DECISION_NOT_ALLOWED = "decision_not_allowed"
    PAYLOAD_REQUIRED = "payload_required"
    PAYLOAD_INVALID = "payload_invalid"
    NO_PENDING_INTERRUPT = "no_pending_interrupt"
    DUPLICATE_RESUME_INTERRUPT_ID = "duplicate_resume_interrupt_id"
    UNKNOWN_INTERRUPT_ID = "unknown_interrupt_id"
    INCOMPLETE = "incomplete"
    CHECKPOINT_MESSAGES_REQUIRED = "checkpoint_messages_required"


class ResumeMappingError(ValueError):
    """Resume data cannot be translated without losing native semantics."""

    def __init__(self, failure: ResumeMappingFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure
        self.message = message


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
    ] = field(default_factory=dict)

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
        if not pending:
            raise ResumeMappingError(
                ResumeMappingFailure.NO_PENDING_INTERRUPT,
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
                    ResumeMappingFailure.DUPLICATE_RESUME_INTERRUPT_ID,
                    f"resume contains duplicate interruptId: {interrupt_id}",
                )
            if interrupt_id not in pending:
                raise ResumeMappingError(
                    ResumeMappingFailure.UNKNOWN_INTERRUPT_ID,
                    f"resume contains unknown interruptId: {interrupt_id}",
                )
            received_ids.add(interrupt_id)
            pending_action = pending[interrupt_id]
            if entry.status == "cancelled":
                if entry.payload is not None:
                    raise ResumeMappingError(
                        ResumeMappingFailure.PAYLOAD_INVALID,
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
                ResumeMappingFailure.INCOMPLETE,
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
            resolved_slots = tuple(
                tuple(decision is not None for decision in decisions)
                for decisions in grouped_decisions.values()
            )
            prior_tool_call_ids = self._prior_tool_call_ids(
                action_groups,
                messages_by_namespace,
                selected_slots=resolved_slots,
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
                    ResumeMappingFailure.INCOMPLETE,
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
        prior_tool_call_ids = self._prior_tool_call_ids(
            action_groups,
            messages_by_namespace,
        )
        return ResumeTranslation(
            mode="command",
            resume_data=resume_data,
            prior_tool_call_ids=prior_tool_call_ids,
            decisions_by_interrupt={
                interrupt_id: tuple(decisions)
                for interrupt_id, decisions in grouped_decisions.items()
            },
        )

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
                ResumeMappingFailure.CHECKPOINT_MESSAGES_REQUIRED,
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
                ResumeMappingFailure.INTERRUPT_UNSUPPORTED,
                "checkpoint messages do not have valid graph namespaces or Tool IDs",
            ) from error
        try:
            matched_groups = match_hitl_tool_call_id_groups(
                action_groups,
                scoped_messages,
                selected_slots=selected_slots,
            )
        except HitlCorrelationError as error:
            raise ResumeMappingError(
                ResumeMappingFailure.INTERRUPT_UNSUPPORTED,
                "checkpoint review actions cannot be correlated to unique Tool calls",
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
                    ResumeMappingFailure.INTERRUPT_UNSUPPORTED,
                    "the thread contains an unsupported interrupt type",
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
                        ResumeMappingFailure.DUPLICATE_PENDING_INTERRUPT_ID,
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
        if entry.payload is None:
            raise ResumeMappingError(
                ResumeMappingFailure.PAYLOAD_REQUIRED,
                f"interruptId={entry.interrupt_id} requires a payload",
            )
        if not isinstance(entry.payload, Mapping):
            ResumeMapper._raise_invalid_payload(entry.interrupt_id)
        payload = dict(entry.payload)
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
            if set(edited_action) != {"name", "args"}:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            name = edited_action.get("name")
            args = edited_action.get("args")
            if not isinstance(name, str) or not name or not isinstance(args, Mapping):
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            # Deep Agents does not re-run review policy after an edit, so the Tool
            # identity is fixed and only its arguments may change.
            if name != pending.action_name:
                ResumeMapper._raise_invalid_payload(entry.interrupt_id)
            try:
                normalized = normalize_operational_data(args)
                normalized_args = JsonObject.model_validate(normalized).root
            except (TypeError, ValueError) as error:
                raise ResumeMappingError(
                    ResumeMappingFailure.PAYLOAD_INVALID,
                    f"interruptId={entry.interrupt_id} has an invalid payload",
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
            ResumeMappingFailure.PAYLOAD_INVALID,
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
            ResumeMappingFailure.DECISION_NOT_ALLOWED,
            f"interruptId={interrupt_id} does not allow {decision}",
        )
