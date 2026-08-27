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
    Identity,
    ResumeMapper,
    ResumeMappingError,
    ResumeTranslation,
    ScopedIdCodec,
)

from ._hitl import CANCEL_DECISION_TYPE
from .errors import AgUiResumeBindingError

RESUME_MARKER_STATE_KEY = "_tinkerfin_resume"


def _to_camel(value: str) -> str:
    """Return the package's stable lower-camel JSON field name."""

    words = value.split("_")
    return "".join(
        word if index == 0 else word.capitalize() for index, word in enumerate(words)
    )


class _ResumeMarker(BaseModel):
    """Validated private checkpoint evidence for one accepted resume command."""

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
    """Stable evidence delivered after a resume marker is checkpointed.

    Hosts must consume this value idempotently. A retry intentionally delivers the
    same marker again before continuation output, but never resubmits its native
    decision.
    """

    identity: Identity
    parent_run_id: str | None
    marker_id: str
    native_interrupt_ids: frozenset[str]


AgUiResumeCheckpointObserver: TypeAlias = Callable[
    [AgUiResumeCheckpoint], Awaitable[None]
]


class AgUiResumeBinding(BaseModel):
    """Persist validated resume and cancellation facts without run identity.

    Use :meth:`from_agui` for the common host path. The model owns a stable JSON
    round trip for hosts that need to persist it, but it owns no graph, checkpointer,
    identity, parent lineage, callback, or I/O resource. Native ``Command`` creation
    remains private to the Runtime.
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
        identity: Identity,
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

    def _invocation_command(
        self,
        *,
        identity: Identity,
        parent_run_id: str | None,
        state_update: Mapping[str, object],
    ) -> Command[object]:
        """Return the atomic private-marker and native-resume command."""

        marker = self._marker(identity=identity, parent_run_id=parent_run_id)
        resume_data = self._resume_snapshot()
        if resume_data is None:  # pragma: no cover - protected by marker validation
            raise RuntimeError("resume binding lost its native data")
        update = dict(state_update)
        if RESUME_MARKER_STATE_KEY in update:
            raise ValueError("state_update cannot replace the private resume marker")
        update[RESUME_MARKER_STATE_KEY] = marker.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
        return Command(
            update=update,
            resume=resume_data,
        )

    def _checkpoint(
        self,
        *,
        identity: Identity,
        parent_run_id: str | None,
    ) -> AgUiResumeCheckpoint:
        """Return stable host evidence for a durably accepted resume."""

        marker = self._marker(identity=identity, parent_run_id=parent_run_id)
        return AgUiResumeCheckpoint(
            identity=identity,
            parent_run_id=parent_run_id,
            marker_id=marker.digest,
            native_interrupt_ids=frozenset(self.native_interrupt_ids),
        )

    def _matches_marker(
        self,
        value: object,
        *,
        identity: Identity,
        parent_run_id: str | None,
    ) -> bool:
        """Return whether persisted marker data proves this exact resume."""

        try:
            persisted = _ResumeMarker.model_validate(value)
        except ValidationError:
            return False
        return persisted == self._marker(
            identity=identity,
            parent_run_id=parent_run_id,
        )

    @staticmethod
    def _validate_scope(
        *,
        identity: Identity,
        parent_run_id: str | None,
    ) -> None:
        """Validate the run facts combined with this identity-free binding."""

        if not isinstance(identity, Identity):
            raise TypeError("identity must be an Identity")
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
    "parse_resume_marker",
]
