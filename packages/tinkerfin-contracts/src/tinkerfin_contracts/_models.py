"""Shared validation behavior for protocol-neutral contract values."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel


class ContractModel(BaseModel):
    """Provide strict immutable validation for cross-package boundary values."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class ObservationModel(ContractModel):
    """Attach comparable wall and monotonic timestamps to one observation."""

    observed_at: datetime
    monotonic_ns: int

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        """Require an aware UTC timestamp without rewriting caller evidence."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("observed_at must be an aware UTC timestamp")
        return value

    @field_validator("monotonic_ns")
    @classmethod
    def monotonic_ns_is_non_negative(cls, value: int) -> int:
        """Reject invalid monotonic offsets before observers compare durations."""

        if value < 0:
            raise ValueError("monotonic_ns must be non-negative")
        return value


__all__ = ["ContractModel", "ObservationModel"]
