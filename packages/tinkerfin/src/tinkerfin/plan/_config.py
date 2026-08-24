"""Immutable configuration and run-mode contracts for Plan-capable agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from langchain_core.language_models import BaseChatModel

from ._clarification import ClarificationSchemaBinding
from ._content import PlanContentBinding
from ._contracts import PlanContractBinding
from .errors import PlanModeConfigurationError

AgentMode: TypeAlias = Literal["default", "plan"]


@dataclass(frozen=True, slots=True)
class PlanOptions:
    """Definition-level options shared by every run of one Plan-capable agent."""

    clarification: ClarificationSchemaBinding
    content: PlanContentBinding
    contracts: PlanContractBinding
    default_mode: AgentMode = "default"
    planner_model: str | BaseChatModel | None = None


def validate_agent_mode(value: object, *, name: str = "mode") -> AgentMode:
    """Return one supported mode or raise a public configuration error."""

    if value not in ("default", "plan"):
        raise PlanModeConfigurationError(f"{name} must be either 'default' or 'plan'")
    return value


def resolve_agent_mode(
    value: object | None,
    *,
    options: PlanOptions | None,
) -> AgentMode:
    """Resolve one request mode against immutable Definition capabilities."""

    if value is None:
        mode: AgentMode = "default" if options is None else options.default_mode
    else:
        mode = validate_agent_mode(value)
    if mode == "plan" and options is None:
        raise PlanModeConfigurationError(
            "mode='plan' requires a Plan-capable Deep Agent Definition"
        )
    return mode


__all__ = [
    "AgentMode",
    "PlanOptions",
    "resolve_agent_mode",
    "validate_agent_mode",
]
