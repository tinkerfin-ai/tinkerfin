"""Read-only Planner agent used by the standalone Planning workflow."""

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
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.typing import ContextT

from ._clarification import ClarificationSchemaBinding, stateless_child_config
from ._content import PlanContentBinding
from ._contracts import PlanContractBinding, PlannerOutcomeBase
from .errors import PlanStructuredOutputError
from .models import PlanContentModel, PlanState

_READ_ONLY_TOOLS: list[FsToolName] = [
    "ls",
    "read_file",
    "glob",
    "grep",
]
_PLANNER_MODEL_CALL_LIMIT = 6
_PLANNER_PROMPT = """You are the single read-only Planner for a user-reviewed workflow.

You create a Plan for a separate execution Deep Agent. Your deliberately restricted
tool list exists only for optional workspace inspection; it neither describes nor
limits the execution Agent's tools. Preserve explicitly requested execution tools and
capabilities in the draft even when they are absent here. Never call, simulate, or test
an execution tool yourself, and never claim it is unavailable merely because the
Planner does not bind it.

Use read-only filesystem tools only when existing workspace evidence can materially
change the Plan. Start with one targeted listing, read, or search. If that inspection
shows no relevant artifact, stop inspecting; do not broaden the search, repeat an
equivalent query, or guess file paths. State the resulting assumption in the draft.
Spend no more than three model turns on filesystem inspection, then return the
structured outcome.

First decide whether the user's intent and constraints are sufficient for an executable
Plan. When additional information is useful, return one non-empty clarification form
that conforms to the configured structured response schema. Set required=true only when
planning cannot safely continue without that answer. Set required=false for useful but
non-blocking refinements the user may skip. A form may contain only optional questions,
but do not pause merely to collect low-value detail. Reassess sufficiency after every
complete answer batch; multiple clarification rounds are allowed.

An explicitly skipped optional question means the user chose not to provide that detail.
Do not ask the same optional question again in this Plan cycle. Continue from available
evidence and state any material assumption in the draft unless a different required
blocker is discovered.

The trusted context can contain an authoritativeEdit. It is user-authored and must never
be silently rewritten. When an authoritativeEdit is present, return clarify if it is
still insufficient, or accept_edit when it is sufficient. Do not return a replacement
draft for an authoritative edit. Without an authoritativeEdit, return clarify or one
complete draft conforming exactly to the configured Plan content schema. Treat that
schema and its field descriptions as the authoritative content contract.

Never claim to have modified state and never request a write or execution tool. Do not
expose private chain-of-thought. Choose each question's semantic answer type only from
the configured types listed below. Choice options must be concise and stable within the
form. Allow custom text only when it can safely express a valid alternative.
"""


class _StructuredAgent(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, object],
        config: RunnableConfig | None = None,
    ) -> Mapping[str, object]: ...


def _planner_system_prompt(
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
) -> str:
    """Add guidance derived from both configured structured response schemas."""

    count = clarification.question_count
    if count.maximum is None:
        cardinality = (
            f"at least {count.minimum} "
            f"{'question' if count.minimum == 1 else 'questions'} and sets no maximum"
        )
    elif count.minimum == count.maximum:
        cardinality = (
            f"exactly {count.minimum} "
            f"{'question' if count.minimum == 1 else 'questions'}"
        )
    else:
        cardinality = (
            f"between {count.minimum} and {count.maximum} questions, inclusive"
        )
    instruction = (
        "Whenever you return clarify, the configured clarification schema requires "
        f"{cardinality}."
    )
    if content.reference.media_type == "text/markdown":
        content_instruction = (
            "Whenever you return draft, put one complete, executable Markdown Plan in "
            "the markdown field. Preserve requested implementation boundaries and "
            "include observable verification and final acceptance conditions in that "
            "Markdown; do not wrap it in a JSON code fence."
        )
    else:
        content_instruction = (
            "Whenever you return draft, satisfy every required field and constraint "
            "of the configured Plan content schema."
        )
    type_descriptions = "\n".join(
        f"- {type_id}: {clarification.types[type_id].description}"
        for type_id in sorted(clarification.types)
    )
    type_instruction = (
        "\n\nThe configured form supports only these semantic answer types:\n"
        f"{type_descriptions}"
    )
    return (
        f"{_PLANNER_PROMPT.rstrip()}\n\n{instruction}\n\n{content_instruction}"
        f"{type_instruction}"
    )


def _invalid_structured_call_messages(
    result: Mapping[str, object],
) -> tuple[BaseMessage, ...] | None:
    """Return validated state only for a provider-invalid Planner tool call."""

    raw_messages = result.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        return None
    values = cast(Sequence[object], raw_messages)
    messages = tuple(item for item in values if isinstance(item, BaseMessage))
    if len(messages) != len(values):
        return None
    last_ai = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    if last_ai is None or not any(
        call.get("name") == "PlannerOutcome" for call in last_ai.invalid_tool_calls
    ):
        return None
    return messages


def create_planner_agent(
    model: str | BaseChatModel,
    *,
    backend: BackendProtocol,
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
    contracts: PlanContractBinding,
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
            system_prompt=_planner_system_prompt(clarification, content),
            middleware=middleware,
            response_format=ToolStrategy(
                contracts.planner_response_type,
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
    plan: PlanState[PlanContentModel],
    *,
    contracts: PlanContractBinding,
    clarification_history: Sequence[Mapping[str, object]],
    config: RunnableConfig,
    files: object | None,
) -> PlannerOutcomeBase:
    """Run the Planner with current requirements and the previous reviewed draft."""

    context = {
        "clarifications": list(clarification_history),
        "authoritativeEdit": (
            None
            if plan.pending_edit is None
            else plan.pending_edit.model_dump(mode="json", by_alias=True)
        ),
        "previousDraft": (
            None
            if plan.draft is None
            else plan.draft.content.model_dump(mode="json", by_alias=True)
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
    if isinstance(response, contracts.planner_response_type):
        return response

    invalid_messages = _invalid_structured_call_messages(result)
    if invalid_messages is not None:
        retry_input: dict[str, object] = {
            "messages": [
                *invalid_messages,
                HumanMessage(
                    content=(
                        "Your previous PlannerOutcome tool call had invalid JSON "
                        "arguments and was not executed. Return exactly one valid "
                        "PlannerOutcome tool call for the same planning decision. "
                        "Use strict JSON without trailing commas or comments."
                    )
                ),
            ]
        }
        retry_files = result.get("files", files)
        if retry_files is not None:
            retry_input["files"] = retry_files
        result = await agent.ainvoke(
            retry_input,
            config=stateless_child_config(config),
        )
        response = result.get("structured_response")
        if isinstance(response, contracts.planner_response_type):
            return response

    raise PlanStructuredOutputError(
        "Planner did not return the configured structured response type"
    )


__all__ = ["create_planner_agent", "invoke_planner"]
