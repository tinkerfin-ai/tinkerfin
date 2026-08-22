"""Versioned correlation for Tool results emitted outside their source graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .reasoning import normalize_operational_data

TOOL_RESULT_CORRELATION_KEY = "tinkerfinToolResult"
TOOL_RESULT_CORRELATION_SCHEMA = "tinkerfin.tool-result-correlation.v1"


class ToolResultCorrelation(BaseModel):
    """Preserve original scoped IDs when a parent graph completes a child Tool."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_id: Literal["tinkerfin.tool-result-correlation.v1"] = Field(
        alias="schema",
        description="Exact TinkerFin Tool result correlation schema",
    )
    tool_call_id: str = Field(
        alias="toolCallId",
        strict=True,
        min_length=1,
        description="Original namespace-scoped AG-UI Tool call ID",
    )
    parent_message_id: str = Field(
        alias="parentMessageId",
        strict=True,
        min_length=1,
        description="Original namespace-scoped assistant message ID",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_json_graph(cls, value: object) -> object:
        """Reject opaque and non-finite correlation payloads."""

        return normalize_operational_data(value)


__all__ = [
    "TOOL_RESULT_CORRELATION_KEY",
    "TOOL_RESULT_CORRELATION_SCHEMA",
    "ToolResultCorrelation",
]
