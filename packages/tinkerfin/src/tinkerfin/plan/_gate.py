"""Conservative request routing for the parent Plan workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.typing import ContextT

from ._clarification import ClarificationSchemaBinding, stateless_child_config
from ._contracts import GateDecisionBase
from .errors import PlanStructuredOutputError

_GATE_PROMPT = """You are the conservative routing gate for an agent workflow.

Choose exactly one route using the structured response:
- direct: only when the request is fully specified, low risk, and can be completed as
  one obvious action without architectural or multi-step decisions.
- clarify: when missing user information would materially change the correct result.
- plan: for explicit planning requests, multi-step work, risky work, architectural
  decisions, or whenever direct execution is uncertain.

Never expose private chain-of-thought. Return only the operational goal, route, and one
form with up to three blocking questions when clarification is required. For each question, generate
concise single-select options when they can cover the likely choices, and decide whether
free-text input is also safe and useful.
"""


class _StructuredAgent(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, object],
        config: RunnableConfig | None = None,
    ) -> Mapping[str, object]: ...


def create_gate_agent(
    model: str | BaseChatModel,
    *,
    clarification: ClarificationSchemaBinding,
    context_schema: type[ContextT] | None,
) -> _StructuredAgent:
    """Build a stateless structured Gate subgraph that inherits parent resources."""

    return cast(
        _StructuredAgent,
        create_agent(
            model=model,
            tools=(),
            system_prompt=_GATE_PROMPT,
            response_format=ToolStrategy(
                clarification.gate_response_type,
                handle_errors=True,
            ),
            context_schema=context_schema,
            checkpointer=False,
            store=None,
            cache=None,
            name="tinkerfin_plan_gate",
        ),
    )


async def invoke_gate(
    agent: _StructuredAgent,
    messages: Sequence[BaseMessage],
    *,
    clarification: ClarificationSchemaBinding,
    config: RunnableConfig,
    requirement_summary: str | None,
) -> GateDecisionBase:
    """Run the Gate with conversation messages and trusted clarification context."""

    gate_messages = list(messages)
    if requirement_summary:
        gate_messages.append(
            HumanMessage(
                content=(
                    "Trusted clarifications collected by the workflow:\n"
                    f"{requirement_summary}"
                )
            )
        )
    result = await agent.ainvoke(
        {"messages": gate_messages},
        config=stateless_child_config(config),
    )
    response = result.get("structured_response")
    if not isinstance(response, clarification.gate_response_type):
        raise PlanStructuredOutputError(
            "Gate did not return the configured structured response type"
        )
    return response


__all__ = ["create_gate_agent", "invoke_gate"]
