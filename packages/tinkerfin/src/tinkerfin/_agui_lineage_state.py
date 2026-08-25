"""Saver-neutral private state for canonical AG-UI checkpoint lineage."""

from __future__ import annotations

from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from tinkerfin_agui_adapter import Identity

LINEAGE_STATE_KEY = "_tinkerfin_lineage"
PLANNING_CHECKPOINT_RUN_ID = "tinkerfin-plan-v1"

LineageRole: TypeAlias = Literal["native", "planning"]


def _to_camel(value: str) -> str:
    words = value.split("_")
    return "".join(
        word if index == 0 else word.capitalize() for index, word in enumerate(words)
    )


class LineageMarker(BaseModel):
    """Persist one semantic run and graph role across checkpoint savers."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    role: LineageRole

    @field_validator("thread_id", "run_id", "parent_run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject identifiers that cannot produce stable lineage evidence."""

        if value is not None and value != value.strip():
            raise ValueError("lineage marker identifiers must be canonical")
        return value


def lineage_marker(
    *,
    identity: Identity,
    parent_run_id: str | None,
    role: LineageRole,
) -> LineageMarker:
    """Build the private marker for one canonical Graph invocation."""

    return LineageMarker(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        parent_run_id=parent_run_id,
        role=role,
    )


def lineage_state_update(
    *,
    identity: Identity,
    parent_run_id: str | None,
    role: LineageRole,
) -> dict[str, dict[str, JsonValue]]:
    """Return one JSON-safe private state update for a Graph input."""

    marker = lineage_marker(
        identity=identity,
        parent_run_id=parent_run_id,
        role=role,
    )
    return {
        LINEAGE_STATE_KEY: cast(
            dict[str, JsonValue],
            marker.model_dump(mode="json", by_alias=True, exclude_none=False),
        )
    }


def parse_lineage_marker(value: object) -> LineageMarker:
    """Validate one persisted private lineage marker without coercion."""

    return LineageMarker.model_validate(value)


def lineage_marker_with_role(
    value: object,
    *,
    role: LineageRole,
) -> dict[str, JsonValue]:
    """Copy a validated marker while changing only its graph role."""

    marker = parse_lineage_marker(value).model_copy(update={"role": role})
    return cast(
        dict[str, JsonValue],
        marker.model_dump(mode="json", by_alias=True, exclude_none=False),
    )


__all__ = [
    "LINEAGE_STATE_KEY",
    "PLANNING_CHECKPOINT_RUN_ID",
    "LineageMarker",
    "LineageRole",
    "lineage_marker",
    "lineage_marker_with_role",
    "lineage_state_update",
    "parse_lineage_marker",
]
