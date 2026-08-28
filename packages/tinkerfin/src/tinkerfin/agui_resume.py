"""Validated, persistable AG-UI resume facts owned by the Runtime boundary."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from ag_ui.core.types import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import BaseMessage
from langgraph.types import Command
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from tinkerfin_agui_adapter import (
    AgentRuntimeInterrupt,
    ResumeMapper,
    ResumeMappingError,
    ResumeTranslation,
    ScopedIdCodec,
)
from tinkerfin_contracts import RunIdentity, RunResumeSummary

from ._agui_lineage_state import RESUME_MARKER_STATE_KEY
from ._hitl import CANCEL_DECISION_TYPE
from .errors import AgUiResumeBindingError

_ResumeDecision: TypeAlias = Literal["approve", "edit", "reject", "respond"]


def _to_camel(value: str) -> str:
    """Return the package's stable lower-camel JSON field name."""

    words = value.split("_")
    return "".join(
        word if index == 0 else word.capitalize() for index, word in enumerate(words)
    )


class AgUiResumeRequest(BaseModel):
    """Carry only untrusted client decisions into framework checkpoint resolution.

    The request contains no server interrupt payload, native command, checkpoint
    identity, or Runtime Profile selection. A Definition resolves those facts from its
    concrete checkpointer before producing :class:`AgUiResumeBinding`.
    """

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    entries: tuple[ResumeEntry, ...] = Field(min_length=1)

    @field_validator("entries", mode="before")
    @classmethod
    def entries_are_an_immutable_sequence(cls, value: object) -> object:
        """Normalize a JSON array without accepting another container shape."""

        if isinstance(value, list):
            return tuple(cast(list[object], value))
        return value

    @model_validator(mode="after")
    def interrupt_ids_are_unique(self) -> AgUiResumeRequest:
        """Reject duplicate client decisions before checkpoint I/O."""

        identifiers = tuple(entry.interrupt_id for entry in self.entries)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("resume entries must use unique interrupt IDs")
        return self


def _observation_group_summary(
    value: JsonValue | None,
) -> tuple[Literal["resolved", "cancelled"], _ResumeDecision | None]:
    if not isinstance(value, dict):
        return "resolved", None
    raw_decisions = value.get("decisions")
    if not isinstance(raw_decisions, list):
        return "resolved", None
    decision_types = [
        item.get("type")
        for item in raw_decisions
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    ]
    status: Literal["resolved", "cancelled"] = (
        "cancelled" if CANCEL_DECISION_TYPE in decision_types else "resolved"
    )
    decision: _ResumeDecision | None = None
    if len(decision_types) == 1 and decision_types[0] in {
        "approve",
        "edit",
        "reject",
        "respond",
    }:
        decision = cast(_ResumeDecision, decision_types[0])
    return status, decision


class _ResumeMarker(BaseModel):
    """Validated private checkpoint evidence for one saver-readable resume intent."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    native_interrupt_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("native_interrupt_ids", mode="before")
    @classmethod
    def interrupt_ids_are_json_arrays(cls, value: object) -> object:
        """Normalize JSON arrays before strict tuple validation."""

        if isinstance(value, list):
            return tuple(cast(list[object], value))
        return value

    @field_validator("thread_id", "run_id", "parent_run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject marker identities that cannot produce stable checkpoint evidence."""

        if value is not None and value != value.strip():
            raise ValueError("resume marker identifiers must be canonical")
        return value

    @model_validator(mode="after")
    def interrupt_ids_are_canonical(self) -> _ResumeMarker:
        """Require a stable unique native interrupt set."""

        values = self.native_interrupt_ids
        if any(not value or value != value.strip() for value in values):
            raise ValueError("native interrupt IDs must be canonical strings")
        if len(set(values)) != len(values):
            raise ValueError("native interrupt IDs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class AgUiResumeCheckpoint:
    """Stable evidence delivered after a resume marker is saver-readable.

    Hosts must consume this value idempotently. A retry intentionally delivers the
    same prepared marker again before continuation output; once the Graph has accepted
    the native decision, retry continues without resubmitting it.
    """

    identity: RunIdentity
    parent_run_id: str | None
    marker_id: str
    native_interrupt_ids: frozenset[str]


AgUiResumeCheckpointObserver: TypeAlias = Callable[
    [AgUiResumeCheckpoint], Awaitable[None]
]
AgUiResumeInitializationFailureObserver: TypeAlias = Callable[[], Awaitable[None]]


class AgUiResumeBinding(BaseModel):
    """Persist validated resume and cancellation facts without run identity.

    The common host path receives this model from
    :meth:`DeepAgentDefinition.prepare_agui_resume`, which resolves authoritative facts
    from the Graph checkpoint. :meth:`from_agui` remains an advanced boundary for hosts
    that already own a complete trusted AG-UI terminal log. The model has a stable JSON
    round trip, but owns no graph, checkpointer, identity, parent lineage, callback, or
    I/O resource. Native ``Command`` creation remains private to the Runtime.
    """

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    mode: Literal["resume", "abandon"] = Field(
        description="Whether the Graph resumes or the complete batch is abandoned"
    )
    resume_data: dict[str, JsonValue] | None = Field(
        default=None,
        description="Validated native resume object owned by the Runtime",
    )
    native_interrupt_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Complete native interrupt groups covered by this binding",
    )
    prior_tool_call_ids: tuple[str, ...] = Field(
        default=(),
        description="Scoped Tool IDs whose start and arguments were already emitted",
    )
    source_agent_names: tuple[str, ...] = Field(
        default=(),
        description="Named subagents that own reviewed Tools in this batch",
    )
    unidentified_external_source: bool = Field(
        default=False,
        description="Whether reviewed Tools came from an unnamed compiled graph",
    )

    def __getattribute__(self, name: str) -> object:
        """Return native resume data defensively while preserving normal model access."""

        if name == "resume_data":
            values = object.__getattribute__(self, "__dict__")
            return copy.deepcopy(values.get("resume_data"))
        return super().__getattribute__(name)

    @model_validator(mode="before")
    @classmethod
    def snapshot_and_normalize_json(cls, value: object) -> object:
        """Copy caller data and normalize JSON arrays for a frozen boundary value."""

        if not isinstance(value, Mapping):
            return value
        normalized = copy.deepcopy(dict(cast(Mapping[object, object], value)))
        for snake_name, alias in (
            ("native_interrupt_ids", "nativeInterruptIds"),
            ("prior_tool_call_ids", "priorToolCallIds"),
            ("source_agent_names", "sourceAgentNames"),
        ):
            key = alias if alias in normalized else snake_name
            item = normalized.get(key)
            if isinstance(item, list | frozenset):
                normalized[key] = tuple(cast(Sequence[object], item))
        return normalized

    @field_validator(
        "native_interrupt_ids",
        "prior_tool_call_ids",
        "source_agent_names",
    )
    @classmethod
    def strings_are_canonical(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Canonicalize immutable string collections for stable persistence."""

        if any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in value
        ):
            raise ValueError("resume binding identifiers must be canonical strings")
        if len(set(value)) != len(value):
            raise ValueError("resume binding identifiers must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_resume_contract(self) -> AgUiResumeBinding:
        """Require one lossless execution mode and valid scoped Tool identities."""

        if self.mode == "resume" and self.resume_data is None:
            raise ValueError("resume mode requires resumeData")
        if self.mode == "abandon" and self.resume_data is not None:
            raise ValueError("abandon mode cannot include resumeData")
        codec = ScopedIdCodec()
        for tool_call_id in self.prior_tool_call_ids:
            try:
                kind, _namespace, _raw_id = codec.decode(tool_call_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "priorToolCallIds must contain complete scoped Tool IDs"
                ) from error
            if kind != "tool":
                raise ValueError(
                    "priorToolCallIds must contain complete scoped Tool IDs"
                )
        if self.resume_data is not None:
            _contains_cancel_decision(self.resume_data)
        return self

    @property
    def contains_cancellations(self) -> bool:
        """Return whether this binding abandons one or more pending actions."""

        if self.mode == "abandon":
            return True
        resume_data = self._resume_snapshot()
        return _contains_cancel_decision(cast(dict[str, JsonValue], resume_data))

    def _observation_summaries(self) -> tuple[RunResumeSummary, ...]:
        """Return protocol-neutral interaction outcomes for Runtime observers."""

        if self.mode == "abandon":
            return tuple(
                RunResumeSummary(interrupt_id=interrupt_id, status="cancelled")
                for interrupt_id in self.native_interrupt_ids
            )
        snapshot = self._resume_snapshot()
        assert snapshot is not None
        single_group = len(self.native_interrupt_ids) == 1
        summaries: list[RunResumeSummary] = []
        for interrupt_id in self.native_interrupt_ids:
            group = snapshot if single_group else snapshot.get(interrupt_id)
            status, decision = _observation_group_summary(group)
            summaries.append(
                RunResumeSummary(
                    interrupt_id=interrupt_id,
                    status=status,
                    decision=decision,
                )
            )
        return tuple(summaries)

    @classmethod
    def from_native(
        cls,
        *,
        request: AgUiResumeRequest,
        interrupts: Sequence[AgentRuntimeInterrupt],
        messages_by_namespace: Mapping[tuple[str, ...], Sequence[BaseMessage]],
    ) -> AgUiResumeBinding:
        """Build a binding from one framework-resolved checkpoint snapshot.

        Args:
            request: Client decisions with no trusted interrupt payload.
            interrupts: Complete pending native interrupts from the canonical head.
            messages_by_namespace: Complete checkpoint messages used to prove Tool IDs.

        Returns:
            Frozen binding for resume, mixed cancellation, or abandonment.

        Raises:
            TypeError: ``request`` has the wrong public type.
            AgUiResumeBindingError: Coverage, correlation, or decision validation fails.
            ValueError: The lossless translation cannot be executed by TinkerFin.
        """

        if not isinstance(request, AgUiResumeRequest):
            raise TypeError("request must be an AgUiResumeRequest")
        try:
            translation = ResumeMapper().map(
                entries=request.entries,
                interrupts=interrupts,
                messages_by_namespace=messages_by_namespace,
            )
        except ResumeMappingError as error:
            raise AgUiResumeBindingError(
                error.message,
                context={"adapter_code": error.code.value},
                cause=error,
            ) from error
        return cls._from_translation(translation)

    @classmethod
    def from_agui(
        cls,
        *,
        entries: Sequence[ResumeEntry],
        interrupts: Sequence[AgUiInterrupt],
    ) -> AgUiResumeBinding:
        """Build one binding from complete trusted AG-UI resume facts.

        Args:
            entries: Complete caller decisions validated by the AG-UI schema.
            interrupts: Complete server-persisted interrupts emitted by the prior run.

        Returns:
            A frozen binding for resume, mixed cancellation, or full abandonment.

        Raises:
            AgUiResumeBindingError: Coverage, correlation, decision, or Schema validation
                fails at the adapter boundary. The original adapter error remains the
                trusted ``cause``.
            ValueError: The lossless translation cannot be executed by TinkerFin.
        """

        try:
            translation = ResumeMapper().map_agui(
                entries=entries,
                interrupts=interrupts,
            )
        except ResumeMappingError as error:
            raise AgUiResumeBindingError(
                error.message,
                context={"adapter_code": error.code.value},
                cause=error,
            ) from error
        return cls._from_translation(translation)

    @classmethod
    def _from_translation(
        cls,
        translation: ResumeTranslation,
    ) -> AgUiResumeBinding:
        """Convert one validated adapter translation into Runtime-owned facts."""

        if not isinstance(translation, ResumeTranslation):
            raise TypeError("translation must be a ResumeTranslation")
        native_interrupt_ids = tuple(translation.decisions_by_interrupt)
        if translation.mode == "abandon":
            return cls(
                mode="abandon",
                native_interrupt_ids=native_interrupt_ids,
                prior_tool_call_ids=translation.prior_tool_call_ids,
                source_agent_names=translation.source_agent_names,
                unidentified_external_source=translation.unidentified_external_source,
            )
        if translation.mode == "custom":
            if translation.kind != "tool":
                raise ValueError(
                    "mixed runtime interrupts cannot use Tool cancellation"
                )
            serialized_groups = {
                interrupt_id: {
                    "decisions": [
                        (
                            {"type": CANCEL_DECISION_TYPE}
                            if decision is None
                            else decision
                        )
                        for decision in decisions
                    ]
                }
                for interrupt_id, decisions in translation.decisions_by_interrupt.items()
            }
            if not serialized_groups:
                raise ValueError("custom translation contains no native decisions")
            resume_data: object = (
                next(iter(serialized_groups.values()))
                if len(serialized_groups) == 1
                else serialized_groups
            )
        elif translation.resume_data is not None:
            resume_data = translation.root
        else:
            raise ValueError("resume translation contains no native data")
        if not isinstance(resume_data, dict):
            raise TypeError("resume translation must contain a JSON object")
        return cls(
            mode="resume",
            resume_data=cast(dict[str, JsonValue], resume_data),
            native_interrupt_ids=native_interrupt_ids,
            prior_tool_call_ids=translation.prior_tool_call_ids,
            source_agent_names=translation.source_agent_names,
            unidentified_external_source=translation.unidentified_external_source,
        )

    def _resume_snapshot(self) -> dict[str, JsonValue] | None:
        """Return a defensive copy of private native resume data."""

        raw = object.__getattribute__(self, "__dict__").get("resume_data")
        return copy.deepcopy(cast(dict[str, JsonValue] | None, raw))

    def _marker(
        self,
        *,
        identity: RunIdentity,
        parent_run_id: str | None,
    ) -> _ResumeMarker:
        """Build deterministic private evidence for one canonical run scope."""

        self._validate_scope(identity=identity, parent_run_id=parent_run_id)
        if self.mode != "resume":
            raise ValueError("an abandoned resume has no checkpoint marker")
        resume_data = self._resume_snapshot()
        if resume_data is None:  # pragma: no cover - protected by model validation
            raise RuntimeError("resume binding lost its native data")
        digest_payload = {
            "threadId": identity.thread_id,
            "runId": identity.run_id,
            "parentRunId": parent_run_id,
            "nativeInterruptIds": list(self.native_interrupt_ids),
            "priorToolCallIds": list(self.prior_tool_call_ids),
            "sourceAgentNames": list(self.source_agent_names),
            "unidentifiedExternalSource": self.unidentified_external_source,
            "resumeData": resume_data,
        }
        encoded = json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _ResumeMarker(
            digest=hashlib.sha256(encoded).hexdigest(),
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            parent_run_id=parent_run_id,
            native_interrupt_ids=self.native_interrupt_ids,
        )

    def _state_update(
        self,
        *,
        identity: RunIdentity,
        parent_run_id: str | None,
        state_update: Mapping[str, object],
    ) -> dict[str, object]:
        """Return the private state written before a native decision is submitted.

        LangGraph 1.2.10 does not guarantee that ``Command(update=..., resume=...)`` is
        saver-readable before its first streamed part on every supported Python version.
        The selected Runtime Profile therefore persists these private writes on the
        exact interrupted checkpoint before decision submission. The returned mapping
        contains no decision payload and is never exposed through AG-UI or Observation.
        """

        marker = self._marker(identity=identity, parent_run_id=parent_run_id)
        update = dict(state_update)
        if RESUME_MARKER_STATE_KEY in update:
            raise ValueError("state_update cannot replace the private resume marker")
        update[RESUME_MARKER_STATE_KEY] = marker.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
        return update

    def _invocation_command(self) -> Command[object]:
        """Return the native decision submitted from a verified staged checkpoint."""

        resume_data = self._resume_snapshot()
        if resume_data is None:  # pragma: no cover - protected by model validation
            raise RuntimeError("resume binding lost its native data")
        return Command(resume=resume_data)

    def _checkpoint(
        self,
        *,
        identity: RunIdentity,
        parent_run_id: str | None,
    ) -> AgUiResumeCheckpoint:
        """Return stable host evidence for a durably prepared resume intent."""

        marker = self._marker(identity=identity, parent_run_id=parent_run_id)
        return AgUiResumeCheckpoint(
            identity=identity,
            parent_run_id=parent_run_id,
            marker_id=marker.digest,
            native_interrupt_ids=frozenset(self.native_interrupt_ids),
        )

    @staticmethod
    def _validate_scope(
        *,
        identity: RunIdentity,
        parent_run_id: str | None,
    ) -> None:
        """Validate the run facts combined with this identity-free binding."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if parent_run_id is None:
            return
        if not isinstance(parent_run_id, str):
            raise TypeError("parent_run_id must be a string or None")
        if not parent_run_id or parent_run_id != parent_run_id.strip():
            raise ValueError("parent_run_id must be a canonical non-empty string")
        if parent_run_id == identity.run_id:
            raise ValueError("parent_run_id must differ from identity.run_id")


def parse_resume_marker(value: object) -> _ResumeMarker | None:
    """Parse private checkpoint marker data without weakening corrupt-state checks."""

    try:
        return _ResumeMarker.model_validate(value)
    except (TypeError, ValidationError):
        return None


def _contains_cancel_decision(value: object) -> bool:
    """Validate and identify internal cancellation decisions recursively."""

    found = False

    def visit(item: object) -> None:
        nonlocal found
        if isinstance(item, Mapping):
            mapping = cast(Mapping[object, object], item)
            if mapping.get("type") == CANCEL_DECISION_TYPE:
                if set(mapping) != {"type"}:
                    raise ValueError("internal Tool cancellation has unexpected fields")
                found = True
                return
            for nested in mapping.values():
                visit(nested)
        elif isinstance(item, list | tuple):
            for nested in cast(Sequence[object], item):
                visit(nested)

    visit(value)
    return found


__all__ = [
    "RESUME_MARKER_STATE_KEY",
    "AgUiResumeBinding",
    "AgUiResumeCheckpoint",
    "AgUiResumeCheckpointObserver",
    "AgUiResumeInitializationFailureObserver",
    "AgUiResumeRequest",
    "parse_resume_marker",
]
