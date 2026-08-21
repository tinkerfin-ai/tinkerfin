"""Typed parent-state composition for the optional Plan workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NotRequired

from deepagents.graph import DeepAgentState
from deepagents.middleware.filesystem import FilesystemState
from langchain.agents.middleware.todo import PlanningState
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from pydantic import JsonValue

from .._state_schema import (
    StateSchemaCompositionError,
    StateSchemaSource,
    compose_state_schema,
    middleware_state_sources,
    state_schema_field_names,
)
from .errors import PlanModeConfigurationError
from .models import PlanState

PLAN_STATE_KEY = "tinkerfin_plan"


class PlanWorkflowNodeState(DeepAgentState, total=False):
    """Stable node-input subset shared by every parent workflow node."""

    tinkerfin_plan: NotRequired[dict[str, JsonValue]]


def create_plan_state_schema(
    base_schema: type[DeepAgentState] | None,
    *,
    middleware: Sequence[AgentMiddleware],  # pyright: ignore[reportMissingTypeArgument]
) -> type[DeepAgentState]:
    """Compose the stable parent state without compiled-graph introspection."""

    base = DeepAgentState if base_schema is None else base_schema
    try:
        agent_fields = state_schema_field_names(
            AgentState,
            source="AgentState inherited fields",
        )
        deep_agent_fields = state_schema_field_names(
            DeepAgentState,
            source="DeepAgentState inherited fields",
        )
        sources = [StateSchemaSource("Deep Agent base state", base)]
        sources.extend(
            (
                StateSchemaSource(
                    "Deep Agents filesystem state",
                    FilesystemState,
                    agent_fields,
                ),
                StateSchemaSource(
                    "Deep Agents todo state",
                    PlanningState,
                    agent_fields,
                ),
                *middleware_state_sources(
                    middleware,
                    inherited_schema=AgentState,
                ),
                StateSchemaSource(
                    "TinkerFin Plan state",
                    PlanWorkflowNodeState,
                    deep_agent_fields,
                ),
            )
        )
        return compose_state_schema(
            tuple(sources),
            name=f"{base.__name__}WithTinkerFinPlan",
        )
    except StateSchemaCompositionError as error:
        raise PlanModeConfigurationError(
            str(error),
            cause=error,
        ) from error


def read_plan_state(state: Mapping[str, object]) -> PlanState:
    """Return the validated Plan state stored at the reserved root key."""

    value = state.get(PLAN_STATE_KEY)
    if value is None:
        return PlanState()
    return PlanState.model_validate(value)


def plan_state_update(plan: PlanState) -> dict[str, object]:
    """Serialize Plan state to the JSON-only durable checkpoint contract."""

    if not isinstance(plan, PlanState):
        raise TypeError("plan must be a PlanState")
    return {
        PLAN_STATE_KEY: plan.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    }


__all__ = [
    "PLAN_STATE_KEY",
    "PlanWorkflowNodeState",
    "create_plan_state_schema",
    "plan_state_update",
    "read_plan_state",
]
