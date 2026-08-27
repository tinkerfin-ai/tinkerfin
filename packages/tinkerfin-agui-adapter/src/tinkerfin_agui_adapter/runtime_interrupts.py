"""Generic runtime interrupts that map losslessly to AG-UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, cast

from ag_ui.core import Interrupt as AgUiInterrupt
from pydantic import Field, JsonValue, StringConstraints, field_validator

from ._json_schema import SchemaError, require_valid_schema
from .models import AgentRuntimeInterrupt, RuntimeModel
from .reasoning import sanitize_public_data

RUNTIME_INTERRUPT_SCHEMA = "tinkerfin.runtime-interrupt"
_RUNTIME_INTERRUPT_SCHEMA_FAMILY = f"{RUNTIME_INTERRUPT_SCHEMA}."


class RuntimeInterruptEnvelope(RuntimeModel):
    """Serializable human-input request emitted by a framework workflow."""

    schema_id: Literal["tinkerfin.runtime-interrupt"] = Field(
        default=RUNTIME_INTERRUPT_SCHEMA,
        alias="schema",
        description="Current runtime interrupt envelope contract",
    )
    kind: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            pattern=r"^(?:tool_call|input_required|confirmation|[a-z][a-z0-9._-]*:[a-z][a-z0-9._-]*)$",
        ),
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

    @field_validator("response_schema")
    @classmethod
    def response_schema_is_valid(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Reject malformed response contracts before publishing an interrupt."""

        try:
            require_valid_schema(value)
        except SchemaError as error:
            raise ValueError("response_schema is not valid") from error
        return value


class PersistedRuntimeInterrupt(RuntimeModel):
    """Correlation persisted in an AG-UI interrupt for a later request."""

    schema_id: Literal["tinkerfin.runtime-interrupt"] = Field(
        default=RUNTIME_INTERRUPT_SCHEMA,
        alias="schema",
        description="Current persisted runtime correlation contract",
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
    if not isinstance(schema, str) or (
        schema != RUNTIME_INTERRUPT_SCHEMA
        and not schema.startswith(_RUNTIME_INTERRUPT_SCHEMA_FAMILY)
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

    published = envelope.model_copy(deep=True)
    require_valid_schema(published.response_schema)
    public_value = sanitize_public_data(
        published.model_dump(mode="python", by_alias=True, exclude_none=False)
    )
    if not isinstance(public_value, dict):
        raise TypeError("runtime interrupt envelope must serialize to an object")
    correlation = PersistedRuntimeInterrupt(
        native_interrupt_id=native.id,
        envelope=published,
    )
    return AgUiInterrupt(
        id=native.id,
        reason=published.kind,
        message=published.message,
        response_schema=published.response_schema,
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
