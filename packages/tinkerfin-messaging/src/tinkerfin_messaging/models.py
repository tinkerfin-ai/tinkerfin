"""Versioned public values stored and replayed by messaging backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._identity import required_identifier

ReplayT = TypeVar("ReplayT")


class MessageEnvelope(BaseModel):
    """One immutable payload committed to an ordered channel stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(
        default=1,
        description="Envelope schema version used to decode the durable record.",
    )
    channel: str = Field(
        min_length=1,
        max_length=1024,
        description="Logical channel whose codec interprets the payload.",
    )
    stream: str = Field(
        min_length=1,
        max_length=1024,
        description="Caller-defined ordering and producer-concurrency scope.",
    )
    seq: int = Field(
        ge=1,
        description="One-based monotonic sequence within the channel stream.",
    )
    message_id: str = Field(
        min_length=1,
        max_length=1024,
        description="Stable idempotency identifier within the channel stream.",
    )
    run: str = Field(
        min_length=1,
        max_length=1024,
        description="Caller-defined producer run correlated with the payload.",
    )
    codec: str = Field(
        min_length=1,
        max_length=1024,
        description="Stable persisted schema identifier required for decoding.",
    )
    payload: bytes = Field(
        description="Protocol-neutral encoded payload committed by the backend.",
    )
    created_at: datetime = Field(
        description="Aware UTC timestamp allocated on the first successful commit.",
    )

    @field_validator("channel", "stream", "message_id", "run", "codec")
    @classmethod
    def _identifier_is_canonical(cls, value: str) -> str:
        return required_identifier("identifier", value)

    @field_validator("created_at")
    @classmethod
    def _created_at_is_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("created_at must be an aware UTC timestamp")
        return value


class RecoveryCheckpoint(BaseModel):
    """Opaque source position atomically committed with a recoverable message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(
        default=1,
        description="Checkpoint envelope schema version.",
    )
    position: bytes = Field(
        description="Opaque position interpreted only by the source factory.",
    )
    last_message_id: str | None = Field(
        default=None,
        max_length=1024,
        description="Last stable message ID included in this checkpoint.",
    )

    @field_validator("last_message_id")
    @classmethod
    def _last_message_id_is_canonical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return required_identifier("last_message_id", value)


@dataclass(frozen=True, slots=True)
class DecodedMessage(Generic[ReplayT]):
    """Pair one durable envelope with its codec-decoded value."""

    envelope: MessageEnvelope
    data: ReplayT


@dataclass(frozen=True, slots=True)
class RecoverableMessage(Generic[ReplayT]):
    """Carry one stable message ID and the checkpoint committed after it."""

    message_id: str
    data: ReplayT
    checkpoint: RecoveryCheckpoint

    def __post_init__(self) -> None:
        required_identifier("message_id", self.message_id)
        if self.checkpoint.last_message_id != self.message_id:
            raise ValueError("checkpoint.last_message_id must match message_id")
