"""Read-only Planner agent used by the parent Plan workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.typing import ContextT

from ._contracts import PlannerOutcome
from .models import PlanState

_READ_ONLY_TOOLS: list[FsToolName] = [
    "ls",
    "read_file",
    "glob",
    "grep",
]
_PLANNER_PROMPT = """You are the read-only Planner for a user-reviewed workflow.

Inspect relevant files with the available read-only tools before proposing changes.
Never claim to have modified state and never request a write or execution tool. Return
either blocking clarification questions or one complete structured draft with ordered,
independently verifiable steps and final acceptance criteria. Do not expose private
chain-of-thought. For each blocking question, generate concise single-select options
when they can cover the likely choices, and decide whether a custom answer is also safe
and useful.
"""


class _StructuredAgent(Protocol):
    async def ainvoke(self, input: Mapping[str, object]) -> Mapping[str, object]: ...


def create_planner_agent(
    model: str | BaseChatModel,
    *,
    backend: BackendProtocol,
    context_schema: type[ContextT] | None,
) -> _StructuredAgent:
    """Build a Planner with an explicit read-only filesystem action space."""

    filesystem = FilesystemMiddleware[ContextT, object](
        backend=backend,
        tools=_READ_ONLY_TOOLS,
    )
    return cast(
        _StructuredAgent,
        create_agent(
            model=model,
            tools=(),
            system_prompt=_PLANNER_PROMPT,
            middleware=(filesystem,),
            response_format=ToolStrategy(
                PlannerOutcome.model_json_schema(by_alias=True)
            ),
            context_schema=context_schema,
            checkpointer=None,
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
    files: object | None,
) -> PlannerOutcome:
    """Run the Planner with current requirements and the previous reviewed draft."""

    context = {
        "goal": plan.goal,
        "requirements": [
            answer.model_dump(mode="json", by_alias=True)
            for answer in plan.requirements
        ],
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
    result = await agent.ainvoke(planner_input)
    response = result.get("structured_response")
    return PlannerOutcome.model_validate(response)


__all__ = ["create_planner_agent", "invoke_planner"]
