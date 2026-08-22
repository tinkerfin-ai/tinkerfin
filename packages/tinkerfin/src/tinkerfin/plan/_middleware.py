"""Deep Agent middleware that applies an approved Plan without message pollution."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Generic, cast

from langchain.agents.middleware.todo import (
    PlanningState,
    Todo,
    TodoListMiddleware,
    WriteTodosInput,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from langgraph.typing import ContextT

from tinkerfin_agui_adapter import (
    TOOL_RESULT_CORRELATION_KEY,
    TOOL_RESULT_CORRELATION_SCHEMA,
    ScopedIdCodec,
    ToolResultCorrelation,
)

from ._state import (
    PLAN_EXECUTION_YIELD_KEY,
    PLAN_TODO_CORRELATION_KEY,
    read_plan_state,
)


def _todo_result_correlation(
    runtime: ToolRuntime[ContextT, PlanningState[object]],
    messages: Sequence[BaseMessage],
) -> ToolResultCorrelation:
    """Bind one parent-completed Tool result to its original v2 message scope."""

    execution = runtime.execution_info
    if execution is None or not execution.checkpoint_ns:
        raise RuntimeError("write_todos requires LangGraph execution metadata")
    # LangGraph 1.2.10 message streaming removes the current node component from
    # ExecutionInfo.checkpoint_ns. The contract test compares this projection to
    # the emitted v2 namespace and must be revisited with a runtime upgrade.
    namespace_parts = execution.checkpoint_ns.split("|")
    namespace = tuple(namespace_parts[:-1])
    if not namespace or any(not part for part in namespace):
        raise RuntimeError("write_todos requires a non-root execution namespace")
    tool_call_id = runtime.tool_call_id
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise RuntimeError("write_todos requires a stable Tool call ID")
    if not messages or not isinstance(messages[-1], AIMessage):
        raise RuntimeError("write_todos requires its proposing assistant message")
    assistant = messages[-1]
    if not isinstance(assistant.id, str) or not assistant.id:
        raise RuntimeError("write_todos requires a stable assistant message ID")
    if sum(call.get("id") == tool_call_id for call in assistant.tool_calls) != 1:
        raise RuntimeError("write_todos Tool call is missing or duplicated")
    codec = ScopedIdCodec()
    return ToolResultCorrelation(
        schema=TOOL_RESULT_CORRELATION_SCHEMA,
        toolCallId=codec.encode("tool", namespace, tool_call_id),
        parentMessageId=codec.encode("message", namespace, assistant.id),
    )


def _yield_parent_state_todos(
    runtime: ToolRuntime[ContextT, PlanningState[object]],
    todos: list[Todo],
) -> Command[object]:
    """Commit execution telemetry to the Plan parent before the child continues."""

    state = cast(Mapping[str, object], runtime.state)
    raw_messages = state.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise TypeError("write_todos requires the current execution messages")
    message_values = cast(Sequence[object], raw_messages)
    messages = tuple(
        message for message in message_values if isinstance(message, BaseMessage)
    )
    if len(messages) != len(message_values):
        raise TypeError("write_todos execution messages must be LangChain objects")
    correlation = _todo_result_correlation(runtime, messages)
    update = dict(state)
    update.update(
        {
            "messages": list(messages),
            "todos": todos,
            PLAN_EXECUTION_YIELD_KEY: True,
            PLAN_TODO_CORRELATION_KEY: correlation.model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    )
    return Command(graph=Command.PARENT, update=update)


async def _ayield_parent_state_todos(
    runtime: ToolRuntime[ContextT, PlanningState[object]],
    todos: list[Todo],
) -> Command[object]:
    """Commit execution telemetry from the asynchronous Tool boundary."""

    return _yield_parent_state_todos(runtime, todos)


def _commit_parent_state_todos(
    runtime: ToolRuntime[ContextT, PlanningState[object]],
    todos: list[Todo],
) -> Command[object]:
    """Complete the Tool lifecycle while committing the root todo state."""

    state = cast(Mapping[str, object], runtime.state)
    correlation = ToolResultCorrelation.model_validate(
        state.get(PLAN_TODO_CORRELATION_KEY)
    )
    raw_tool_call_id = runtime.tool_call_id
    if not isinstance(raw_tool_call_id, str) or not raw_tool_call_id:
        raise RuntimeError("write_todos requires a stable Tool call ID")
    kind, _namespace, correlated_raw_id = ScopedIdCodec().decode(
        correlation.tool_call_id
    )
    if kind != "tool" or correlated_raw_id != raw_tool_call_id:
        raise RuntimeError("write_todos parent correlation does not match its Tool ID")
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"Updated todo list to {todos}",
                    id=f"{runtime.tool_call_id}:tinkerfin-plan-todo-result",
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        TOOL_RESULT_CORRELATION_KEY: correlation.model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    },
                )
            ],
            "todos": todos,
            PLAN_EXECUTION_YIELD_KEY: False,
            PLAN_TODO_CORRELATION_KEY: None,
        }
    )


async def _acommit_parent_state_todos(
    runtime: ToolRuntime[ContextT, PlanningState[object]],
    todos: list[Todo],
) -> Command[object]:
    """Complete the asynchronous parent Tool lifecycle."""

    return _commit_parent_state_todos(runtime, todos)


class ParentStateTodoListMiddleware(TodoListMiddleware):
    """Project Plan main-execution todos through the public parent Command path."""

    parent_tool: BaseTool

    def __init__(self, source: TodoListMiddleware) -> None:
        super().__init__(
            system_prompt=source.system_prompt,
            tool_description=source.tool_description,
        )
        self.tools = [
            StructuredTool.from_function(
                name="write_todos",
                description=source.tool_description,
                func=_yield_parent_state_todos,
                coroutine=_ayield_parent_state_todos,
                args_schema=WriteTodosInput,
                infer_schema=False,
            )
        ]
        self.parent_tool = StructuredTool.from_function(
            name="write_todos",
            description=source.tool_description,
            func=_commit_parent_state_todos,
            coroutine=_acommit_parent_state_todos,
            args_schema=WriteTodosInput,
            infer_schema=False,
        )

    @property
    def name(self) -> str:
        """Preserve the upstream middleware node and observability names."""

        return "TodoListMiddleware"


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
            "Plan review is complete and approval has already been granted. Begin "
            "execution now with the tools bound to this execution Agent. Do not "
            "restate the draft, ask for Plan approval again, or wait for another "
            "general approval. Tool-specific human review configured by the runtime "
            "remains mandatory. Execute the following user-approved Plan as the "
            "governing task contract. "
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


__all__ = ["ConfirmedPlanMiddleware", "ParentStateTodoListMiddleware"]
