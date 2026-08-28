"""Canonical run identity shared by TinkerFin framework integrations."""

from __future__ import annotations

from pydantic import Field, field_validator

from ._models import ContractModel


class RunIdentity(ContractModel):
    """Identify one semantic run within one durable conversation thread."""

    thread_id: str = Field(
        alias="threadId",
        min_length=1,
        max_length=1024,
        description="Stable thread identity shared by runtime and durable systems",
    )
    run_id: str = Field(
        alias="runId",
        min_length=1,
        max_length=1024,
        description="Idempotent identity for one semantic run within the thread",
    )

    @field_validator("thread_id", "run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        """Reject surrounding whitespace before the identity causes side effects."""

        if value != value.strip():
            raise ValueError(
                "run identity values must not contain surrounding whitespace"
            )
        return value


__all__ = ["RunIdentity"]
