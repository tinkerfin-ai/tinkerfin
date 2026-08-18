"""Stable protocol projection of Deep Agents subagent task input."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SubagentTaskInput(BaseModel):
    """Minimal locked projection of the Deep Agents `task` Tool input."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        min_length=1,
        description="Task description passed to the subagent",
    )
    subagent_type: str = Field(
        min_length=1,
        description="Target subagent type",
    )
