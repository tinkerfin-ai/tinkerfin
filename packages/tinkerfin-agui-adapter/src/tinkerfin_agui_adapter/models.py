"""Stable JSON and interrupt models used by the stream adapter."""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator
from pydantic.alias_generators import to_camel

from .reasoning import normalize_operational_data


class RuntimeModel(BaseModel):
    """Common strict configuration for adapter boundary models."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
    )


class JsonObject(RootModel[dict[str, JsonValue]]):
    """JSON object validated before crossing a public boundary."""


def to_json_value(value: object) -> JsonValue:
    """Normalize an auditable JSON value without stringifying opaque objects."""

    return normalize_operational_data(value)


class AgentRuntimeInterrupt(RuntimeModel):
    """Pure projection of a pending interrupt supplied by a host runtime.

    The model does not query or own the checkpointer that produced the interrupt.
    """

    id: str = Field(min_length=1, description="Stable interrupt ID")
    value: JsonValue = Field(description="JSON-safe value carried by the interrupt")

    @model_validator(mode="before")
    @classmethod
    def validate_interrupt(cls, value: object) -> object:
        """Extract stable fields from a mapping or framework interrupt object."""

        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            interrupt_id = mapping.get("id")
            interrupt_value = mapping.get("value")
        else:
            interrupt_id = getattr(value, "id", None)
            interrupt_value = getattr(value, "value", None)
        return {
            "id": interrupt_id,
            "value": to_json_value(interrupt_value),
        }
