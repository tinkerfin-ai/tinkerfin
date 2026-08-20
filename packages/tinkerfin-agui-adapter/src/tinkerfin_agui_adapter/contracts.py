"""Main-run outcome contracts for the AG-UI adapter."""

from __future__ import annotations

from typing import Literal

from ag_ui.core import BaseEvent
from ag_ui.core import Interrupt as AgUiInterrupt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import RuntimeModel


class Identity(BaseModel):
    """Canonical thread and run identity shared by TinkerFin integrations."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    thread_id: str = Field(
        alias="threadId",
        min_length=1,
        description="Stable thread identity used by runtime and durable delivery",
    )
    run_id: str = Field(
        alias="runId",
        min_length=1,
        description="Idempotent identity for one semantic run within the thread",
    )

    @field_validator("thread_id", "run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        """Reject surrounding whitespace before the identity causes side effects."""

        if value != value.strip():
            raise ValueError("identity values must not contain surrounding whitespace")
        return value


class AgentRunOutcome(RuntimeModel):
    """Typed success or interrupt outcome returned to a lifecycle owner."""

    type: Literal["success", "interrupt"] = Field(
        description="Main run completion type"
    )
    interrupts: tuple[AgUiInterrupt, ...] = Field(
        default=(),
        description="AG-UI interrupts present when the run pauses",
    )

    @model_validator(mode="after")
    def validate_interrupts(self) -> AgentRunOutcome:
        """Require interrupts exactly when the outcome type is `interrupt`."""

        if self.type == "success" and self.interrupts:
            raise ValueError("success outcome cannot contain interrupts")
        if self.type == "interrupt" and not self.interrupts:
            raise ValueError("interrupt outcome requires interrupts")
        return self


AgentEvent = BaseEvent
