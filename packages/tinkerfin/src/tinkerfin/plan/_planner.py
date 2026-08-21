"""Read-only Planner agent used by the parent Plan workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName
from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.typing import ContextT

from ._clarification import ClarificationSchemaBinding, stateless_child_config
from ._contracts import PlannerOutcomeBase
from .errors import PlanStructuredOutputError
from .models import PlanState

_READ_ONLY_TOOLS: list[FsToolName] = [
    "ls",
    "read_file",
    "glob",
    "grep",
]
_PLANNER_MODEL_CALL_LIMIT = 6
_PLANNER_PROMPT = """You are the read-only Planner for a user-reviewed workflow.

Use read-only filesystem tools only when existing workspace evidence can materially
change the Plan. Start with one targeted listing, read, or search. If that inspection
shows no relevant artifact, stop inspecting; do not broaden the search, repeat an
equivalent query, or guess file paths. State the resulting assumption in the draft.
Spend no more than three model turns on filesystem inspection, then return the
structured outcome.

Never claim to have modified state and never request a write or execution tool. Return
either one clarification form or one complete structured draft with ordered,
independently verifiable steps and final acceptance criteria. Do not expose private
chain-of-thought. For each blocking question, generate concise single-select options
when they can cover the likely choices, and decide whether free-text input is also safe
and useful.
"""


class _StructuredAgent(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, object],
        config: RunnableConfig | None = None,
    ) -> Mapping[str, object]: ...


def create_planner_agent(
    model: str | BaseChatModel,
    *,
    backend: BackendProtocol,
    clarification: ClarificationSchemaBinding,
    context_schema: type[ContextT] | None,
) -> _StructuredAgent:
    """Build a Planner with an explicit read-only filesystem action space."""

    filesystem = FilesystemMiddleware[ContextT, object](
        backend=backend,
        tools=_READ_ONLY_TOOLS,
    )
    # LangChain composes heterogeneous middleware state schemas at runtime, but its
    # invariant generic cannot express their intersection.
    middleware = cast(
        tuple[AgentMiddleware[Any, ContextT, Any], ...],
        (
            filesystem,
            ModelCallLimitMiddleware[ContextT, Any](
                run_limit=_PLANNER_MODEL_CALL_LIMIT,
                exit_behavior="error",
            ),
        ),
    )
    return cast(
        _StructuredAgent,
        create_agent(
            model=model,
            tools=(),
            system_prompt=_PLANNER_PROMPT,
            middleware=middleware,
            response_format=ToolStrategy(
                clarification.planner_response_type,
                handle_errors=True,
            ),
            context_schema=context_schema,
            checkpointer=False,
            store=None,
            cache=None,
            name="tinkerfin_read_only_planner",
        ),
    )


async def invoke_planner(
    agent: _StructuredAgent,
    messages: Sequence[BaseMessage],
    plan: PlanState,
    *,
    clarification: ClarificationSchemaBinding,
    clarification_history: Sequence[Mapping[str, object]],
    config: RunnableConfig,
    files: object | None,
) -> PlannerOutcomeBase:
    """Run the Planner with current requirements and the previous reviewed draft."""

    context = {
        "goal": plan.goal,
        "clarifications": list(clarification_history),
        "previousDraft": (
            None
            if plan.draft is None
            else plan.draft.model_dump(mode="json", by_alias=True)
        ),
        "feedback": list(plan.feedback),
    }
    planner_messages = [
        *messages,
        HumanMessage(
            content=(
                "Trusted Plan workflow context:\n"
                + json.dumps(context, ensure_ascii=False, indent=2)
            )
        ),
    ]
    planner_input: dict[str, object] = {"messages": planner_messages}
    if files is not None:
        planner_input["files"] = files
    result = await agent.ainvoke(
        planner_input,
        config=stateless_child_config(config),
    )
    response = result.get("structured_response")
    if not isinstance(response, clarification.planner_response_type):
        raise PlanStructuredOutputError(
            "Planner did not return the configured structured response type"
        )
    return response


__all__ = ["create_planner_agent", "invoke_planner"]
