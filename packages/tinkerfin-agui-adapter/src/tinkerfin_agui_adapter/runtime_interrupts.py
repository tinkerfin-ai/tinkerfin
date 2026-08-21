"""Generic, versioned runtime interrupts that map losslessly to AG-UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, cast

from ag_ui.core import Interrupt as AgUiInterrupt
from pydantic import Field, JsonValue, StringConstraints

from .models import AgentRuntimeInterrupt, RuntimeModel
from .reasoning import sanitize_public_data

RUNTIME_INTERRUPT_SCHEMA = "tinkerfin.runtime-interrupt.v1"
_RUNTIME_INTERRUPT_SCHEMA_PREFIX = "tinkerfin.runtime-interrupt."


class RuntimeInterruptEnvelope(RuntimeModel):
    """Serializable human-input request emitted by a framework workflow."""

    schema_id: Literal["tinkerfin.runtime-interrupt.v1"] = Field(
        default=RUNTIME_INTERRUPT_SCHEMA,
        alias="schema",
        description="Runtime interrupt envelope schema version",
    )
    kind: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ] = Field(
        description="Stable reason published as the AG-UI interrupt reason",
    )
    message: str | None = Field(
        default=None,
        description="Human-readable request shown by an interrupt client",
    )
    response_schema: dict[str, JsonValue] = Field(
        description="JSON Schema for the resolved AG-UI resume payload"
    )
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Trusted workflow metadata needed to validate a resume",
    )


class PersistedRuntimeInterrupt(RuntimeModel):
    """Correlation persisted in an AG-UI interrupt for a later request."""

    schema_id: Literal["tinkerfin.runtime-interrupt.v1"] = Field(
        default=RUNTIME_INTERRUPT_SCHEMA,
        alias="schema",
        description="Persisted runtime correlation schema version",
    )
    native_interrupt_id: str = Field(
        min_length=1,
        description="Original LangGraph interrupt ID",
    )
    envelope: RuntimeInterruptEnvelope = Field(
        description="Validated runtime interrupt emitted by the graph"
    )


def parse_runtime_interrupt(value: JsonValue) -> RuntimeInterruptEnvelope | None:
    """Parse a declared runtime envelope while leaving unrelated values untouched."""

    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    schema = mapping.get("schema")
    if not isinstance(schema, str) or not schema.startswith(
        _RUNTIME_INTERRUPT_SCHEMA_PREFIX
    ):
        return None
    return RuntimeInterruptEnvelope.model_validate(value)


def prepare_runtime_ag_ui_interrupt(
    native: AgentRuntimeInterrupt,
    envelope: RuntimeInterruptEnvelope,
    *,
    source: Mapping[str, JsonValue],
) -> AgUiInterrupt:
    """Project one validated runtime envelope to one resumable AG-UI interrupt."""

    public_value = sanitize_public_data(
        envelope.model_dump(mode="python", by_alias=True, exclude_none=False)
    )
    if not isinstance(public_value, dict):
        raise TypeError("runtime interrupt envelope must serialize to an object")
    correlation = PersistedRuntimeInterrupt(
        native_interrupt_id=native.id,
        envelope=envelope,
    )
    return AgUiInterrupt(
        id=native.id,
        reason=envelope.kind,
        message=envelope.message,
        response_schema=envelope.response_schema,
        metadata={
            "langgraphValue": public_value,
            "source": dict(source),
            "runtimeInterrupt": correlation.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
        },
    )


def persisted_runtime_interrupt(
    interrupt: AgUiInterrupt,
) -> PersistedRuntimeInterrupt | None:
    """Read trusted runtime correlation from a persisted AG-UI interrupt."""

    metadata = interrupt.metadata
    if not isinstance(metadata, Mapping):
        return None
    raw = cast(Mapping[object, object], metadata).get("runtimeInterrupt")
    if raw is None:
        return None
    persisted = PersistedRuntimeInterrupt.model_validate(raw)
    if interrupt.id != persisted.native_interrupt_id:
        raise ValueError("persisted runtime interrupt ID does not match AG-UI ID")
    if interrupt.reason != persisted.envelope.kind:
        raise ValueError("persisted runtime interrupt kind does not match AG-UI reason")
    return persisted


__all__ = [
    "RUNTIME_INTERRUPT_SCHEMA",
    "PersistedRuntimeInterrupt",
    "RuntimeInterruptEnvelope",
]
