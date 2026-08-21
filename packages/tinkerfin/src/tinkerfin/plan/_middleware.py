"""Deep Agent middleware that applies an approved Plan without message pollution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Generic, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.typing import ContextT

from ._state import read_plan_state


class ConfirmedPlanMiddleware(
    AgentMiddleware[AgentState[object], ContextT, object],
    Generic[ContextT],
):
    """Append the immutable approved Plan to execution model requests."""

    tools = ()

    @staticmethod
    def _request_with_plan(
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        state = cast(Mapping[str, object], request.state)
        plan = read_plan_state(state)
        confirmed = plan.confirmed_plan
        if confirmed is None:
            return request
        instruction = (
            "Execute the following user-approved Plan as the governing task contract. "
            "Use internal todos only as execution telemetry. Do not silently change the "
            "approved goal, steps, assumptions, or acceptance criteria.\n\n"
            + confirmed.model_dump_json(by_alias=True, indent=2)
        )
        if request.system_message is None:
            system_message = SystemMessage(content=instruction)
        else:
            system_message = SystemMessage(
                content_blocks=[
                    *request.system_message.content_blocks,
                    {"type": "text", "text": f"\n\n{instruction}"},
                ]
            )
        return request.override(system_message=system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[object]],
    ) -> ModelResponse[object] | AIMessage:
        """Apply the Plan to a synchronous model call."""

        return handler(self._request_with_plan(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]],
            Awaitable[ModelResponse[object]],
        ],
    ) -> ModelResponse[object] | AIMessage:
        """Apply the Plan to an asynchronous model call."""

        return await handler(self._request_with_plan(request))


__all__ = ["ConfirmedPlanMiddleware"]
