"""Private durable Plan handoff evidence and transient model instruction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NotRequired, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, SystemMessage

PLAN_HANDOFF_STATE_KEY = "_tinkerfin_plan_handoff_digest"


class _PlanHandoffState(AgentState[Any], total=False):
    """Native state contribution proving which approved Plan was staged."""

    _tinkerfin_plan_handoff_digest: NotRequired[str]


_ACTIVE_HANDOFF_INSTRUCTION: ContextVar[str | None] = ContextVar(
    "tinkerfin_active_plan_handoff_instruction",
    default=None,
)


def _request_with_handoff_instruction(
    request: ModelRequest[Any],
) -> ModelRequest[Any]:
    """Append request-scoped approved Plan context without mutating message state."""

    instruction = _ACTIVE_HANDOFF_INSTRUCTION.get()
    if instruction is None:
        return request
    system_message = request.system_message
    if system_message is None:
        updated = SystemMessage(content=instruction)
    else:
        content_blocks = [
            *system_message.content_blocks,
            {"type": "text", "text": f"\n\n{instruction}"},
        ]
        updated = system_message.model_copy(
            update={"content": cast(list[str | dict[str, Any]], content_blocks)}
        )
    return request.override(system_message=updated)


class _PlanHandoffMiddleware(AgentMiddleware[_PlanHandoffState, Any, Any]):
    """Expose approved Plan context only to model calls in the native handoff run."""

    state_schema = _PlanHandoffState
    tools = ()

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage:
        """Inject the active handoff into one synchronous model request."""

        return handler(_request_with_handoff_instruction(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        """Inject the active handoff into one asynchronous model request."""

        return await handler(_request_with_handoff_instruction(request))


def create_plan_handoff_middleware() -> AgentMiddleware[Any, Any, Any]:
    """Create the internal stateless middleware installed on a Plan-capable Agent."""

    return cast(AgentMiddleware[Any, Any, Any], _PlanHandoffMiddleware())


@contextmanager
def activate_plan_handoff_instruction(instruction: str) -> Generator[None]:
    """Scope one approved Plan instruction to the current native execution task."""

    if not isinstance(instruction, str) or not instruction:
        raise ValueError("Plan handoff instruction must be non-empty text")
    token = _ACTIVE_HANDOFF_INSTRUCTION.set(instruction)
    try:
        yield
    finally:
        _ACTIVE_HANDOFF_INSTRUCTION.reset(token)


__all__ = [
    "PLAN_HANDOFF_STATE_KEY",
    "activate_plan_handoff_instruction",
    "create_plan_handoff_middleware",
]
