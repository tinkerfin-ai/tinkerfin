"""Finite replay representation produced by a concrete Native Driver."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from .json import to_json_value
from .stream import NativeStreamMode


class NativeStreamPart(BaseModel):
    """Detached replay and direct-SSE representation of one Native frame.

    A concrete Runtime Profile must create this model while it still owns the live
    upstream object. Persistence and transport consumers receive the already finite
    model and therefore never need to know which third-party stream shape produced it.
    """

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    mode: NativeStreamMode = Field(alias="type")
    namespace: tuple[str, ...] = Field(alias="ns")
    data: JsonValue
    interrupts: tuple[JsonValue, ...] = ()

    @field_validator("data")
    @classmethod
    def _data_is_finite(cls, value: JsonValue) -> JsonValue:
        """Reject non-finite numbers that JSON parsers may otherwise accept."""

        return to_json_value(value)

    @field_validator("interrupts")
    @classmethod
    def _interrupts_are_finite(
        cls,
        value: tuple[JsonValue, ...],
    ) -> tuple[JsonValue, ...]:
        """Keep every persisted interrupt inside the same finite JSON boundary."""

        return tuple(to_json_value(item) for item in value)


__all__ = ["NativeStreamPart"]
