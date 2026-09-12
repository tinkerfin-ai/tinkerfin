"""Prepare workspace access and role-local tools for one admitted Run."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeAlias, cast, get_args

from deepagents import AsyncSubAgent, CompiledSubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.cache.base import BaseCache
from langgraph.checkpoint.base import BaseCheckpointSaver

from tinkerfin_contracts import (
    AgentRunPreparation,
    PreparedWorkspace,
    RunIdentity,
    Workspace,
)

from ._hitl import _as_permissions
from ._middleware_resources import (
    prepare_middleware_resources,
    validate_middleware_resources,
)
from ._run_callbacks import callback_scope
from ._run_resources import RunResources
from ._store import validate_store_backend
from ._store_backend import async_store_backend
from .subagents import SubAgent, WorkspaceT

_BuildBackend: TypeAlias = (
    BackendProtocol | Workspace[WorkspaceT, BackendProtocol] | None
)
_ToolPreparation: TypeAlias = (
    Callable[[AgentRunPreparation[WorkspaceT]], Awaitable[Sequence[BaseTool]]] | None
)
_BuildSubagents: TypeAlias = (
    Sequence[SubAgent[WorkspaceT] | CompiledSubAgent | AsyncSubAgent] | None
)
_BuildTools: TypeAlias = Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None
_BuildCheckpointer: TypeAlias = BaseCheckpointSaver[Any] | bool | None
_BuildCache: TypeAlias = BaseCache[Any] | None
# Prepared business tools must not replace permission-enforced file tools or task
# controls. These names are defined by Deep Agents 0.7.5 FilesystemMiddleware,
# SubAgentMiddleware, AsyncSubAgentMiddleware, and LangChain TodoListMiddleware.
# Reserve them even when a model profile omits a capability in this agent role.
_RESERVED_TOOL_NAMES = frozenset(
    name for name in get_args(FsToolName) if isinstance(name, str)
) | {
    "write_todos",
    "task",
    "start_async_task",
    "check_async_task",
    "update_async_task",
    "cancel_async_task",
    "list_async_tasks",
}


def build_signature(signature: inspect.Signature) -> inspect.Signature:
    """Extend the selected factory with typed per-Run workspace preparation."""

    parameters: list[inspect.Parameter] = []
    for parameter in signature.parameters.values():
        if parameter.name == "backend":
            parameter = parameter.replace(annotation=_BuildBackend)
        elif parameter.name == "subagents":
            parameter = parameter.replace(annotation=_BuildSubagents)
        elif parameter.name == "tools":
            parameter = parameter.replace(annotation=_BuildTools)
        elif parameter.name == "checkpointer":
            parameter = parameter.replace(annotation=_BuildCheckpointer)
        elif parameter.name == "cache":
            parameter = parameter.replace(annotation=_BuildCache)
        parameters.append(parameter)
        if parameter.name == "backend":
            parameters.append(
                inspect.Parameter(
                    "prepare_tools",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=_ToolPreparation,
                )
            )
    return signature.replace(parameters=parameters)


def validate_tool_preparation(arguments: Mapping[str, object]) -> None:
    """Reject invalid preparation declarations without calling providers or tools."""

    prepare = arguments.get("prepare_tools")
    if prepare is not None and not callable(prepare):
        raise TypeError("prepare_tools must be an async callable or None")
    for spec in _subagent_specs(arguments.get("subagents")):
        prepare = spec.get("prepare_tools")
        if prepare is not None:
            if "runnable" in spec or "graph_id" in spec:
                raise ValueError(
                    "prepare_tools is only supported by declarative subagents"
                )
            if not callable(prepare):
                raise TypeError("subagent prepare_tools must be an async callable")


def requires_run_preparation(
    arguments: Mapping[str, object], prepare_tools: object
) -> bool:
    """Identify declarations that cannot be used by an unmanaged reusable Graph."""

    return (
        isinstance(arguments.get("backend"), Workspace)
        or prepare_tools is not None
        or any(
            spec.get("prepare_tools") is not None
            for spec in _subagent_specs(arguments.get("subagents"))
        )
    )


def _subagent_specs(value: object) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("subagents must be a sequence")
    result: list[Mapping[str, object]] = []
    for item in cast(Sequence[object], value):
        if not isinstance(item, Mapping):
            raise TypeError("subagent declarations must be mappings")
        result.append(cast(Mapping[str, object], item))
    return tuple(result)


class _PreparedTools(AgentMiddleware[Any, Any, Any]):
    """Provide only this role's tools, without changing static tool inheritance.

    Deep Agents 0.7.5 graph.create_deep_agent copies parent tools into the default
    general-purpose agent, but only inherits middleware replacing its default slots.
    This distinct middleware slot prevents prepared main tools from leaking to GP.
    """

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        self.tools = tuple(tools)


def _middleware(value: object) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("middleware must be a sequence")
    middleware = tuple(cast(Sequence[object], value))
    if any(not isinstance(item, AgentMiddleware) for item in middleware):
        raise TypeError("middleware must contain AgentMiddleware instances")
    return cast(tuple[AgentMiddleware[Any, Any, Any], ...], middleware)


def validate_agent_middleware(arguments: Mapping[str, object]) -> None:
    """Apply resource inheritance to every declared agent role before execution."""

    validate_middleware_resources(_middleware(arguments.get("middleware")))
    for spec in _subagent_specs(arguments.get("subagents")):
        if "runnable" not in spec and "graph_id" not in spec:
            validate_middleware_resources(_middleware(spec.get("middleware")))


def prepare_agent_middleware(arguments: dict[str, object]) -> None:
    """Bind role-local middleware for managed and directly constructed graphs."""

    arguments["middleware"] = prepare_middleware_resources(
        _middleware(arguments.get("middleware"))
    )
    if arguments.get("subagents") is not None:
        subagents: list[dict[str, object]] = []
        for declaration in _subagent_specs(arguments["subagents"]):
            spec = dict(declaration)
            if "runnable" not in spec and "graph_id" not in spec:
                spec["middleware"] = prepare_middleware_resources(
                    _middleware(spec.get("middleware"))
                )
            subagents.append(spec)
        arguments["subagents"] = subagents


async def _prepare_tools(
    prepare: object,
    context: AgentRunPreparation[object],
) -> tuple[BaseTool, ...]:
    if prepare is None:
        return ()
    if not callable(prepare):
        raise TypeError("prepare_tools must be an async callable")
    # The inspected factory boundary erases the workspace generic. BuildAgent's
    # overloads pair the provider and callback types; this boundary still validates
    # the awaitable and every returned tool before Graph construction.
    function = cast(
        Callable[[AgentRunPreparation[object]], Awaitable[Sequence[BaseTool]]], prepare
    )
    with callback_scope():
        pending = function(context)
        if not inspect.isawaitable(pending):
            raise TypeError("prepare_tools must return an awaitable")
        value: object = await pending
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("prepare_tools must return a sequence of BaseTool instances")
    tools = tuple(cast(Sequence[object], value))
    if any(not isinstance(tool, BaseTool) for tool in tools):
        raise TypeError("prepare_tools must return BaseTool instances")
    return cast(tuple[BaseTool, ...], tools)


def _check_tool_names(
    prepared: Sequence[BaseTool],
    static: object,
    middleware: Sequence[AgentMiddleware[Any, Any, Any]],
) -> None:
    if not prepared:
        return
    if static is None:
        static = ()
    if not isinstance(static, Sequence) or isinstance(static, (str, bytes)):
        raise TypeError("tools must be a sequence")
    # LangChain 1.3.14 create_agent treats middleware.tools as optional. Hooks
    # such as Deep Agents' PatchToolCallsMiddleware do not register tools.
    names = {
        tool.name
        for item in middleware
        for tool in getattr(item, "tools", ())
        if isinstance(tool, BaseTool)
    }
    for tool in cast(Sequence[object], static):
        schema = convert_to_openai_tool(cast(Any, tool))
        function: object = schema.get("function")
        if isinstance(function, Mapping):
            name = cast(Mapping[str, object], function).get("name")
            if isinstance(name, str):
                names.add(name)
    for tool in prepared:
        if tool.name in _RESERVED_TOOL_NAMES:
            raise ValueError(
                f"prepare_tools returned a reserved tool name: {tool.name}"
            )
        if tool.name in names:
            raise ValueError(
                f"prepare_tools returned a duplicate tool name: {tool.name}"
            )
        names.add(tool.name)


async def _role_middleware(
    arguments: Mapping[str, object],
    *,
    static_tools: object,
    prepare_tools: object,
    context: AgentRunPreparation[object],
    workspace: PreparedWorkspace[object, BackendProtocol] | None,
) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
    middleware = _middleware(arguments.get("middleware"))
    if workspace is not None:
        if any(item.name == "FilesystemMiddleware" for item in middleware):
            raise ValueError(
                "a Workspace owns its filesystem middleware; configure permissions instead"
            )
        filesystem = FilesystemMiddleware(
            backend=workspace.backend,
            system_prompt=workspace.filesystem_instructions,
            custom_tool_descriptions=dict(workspace.tool_descriptions),
            _permissions=list(_as_permissions(arguments.get("permissions"))),
        )
        middleware = (*middleware, filesystem)
    tools = await _prepare_tools(prepare_tools, context)
    _check_tool_names(tools, static_tools, middleware)
    if tools:
        middleware = (*middleware, _PreparedTools(tools))
    return middleware


@dataclass(frozen=True, slots=True)
class PreparedAgentArguments:
    """Retain one Run's factory inputs and borrowed workspace information."""

    arguments: dict[str, object]
    workspace: PreparedWorkspace[object, BackendProtocol] | None


async def prepare_agent_arguments(
    arguments: dict[str, object],
    *,
    static_tools: object,
    prepare_tools: object,
    identity: RunIdentity,
    resources: RunResources,
) -> PreparedAgentArguments:
    """Prepare the common workspace, then main and declarative agent tools once."""

    declaration = arguments.get("backend")
    workspace: PreparedWorkspace[object, BackendProtocol] | None = None
    if isinstance(declaration, Workspace):
        workspace = await resources.prepare_workspace(
            cast(Workspace[object, BackendProtocol], declaration), identity
        )
        if not isinstance(workspace, PreparedWorkspace):
            raise TypeError("Workspace.prepare must yield PreparedWorkspace")
        if not isinstance(workspace.backend, BackendProtocol):
            raise TypeError("the prepared backend must implement BackendProtocol")
        validate_store_backend(workspace.backend)
        workspace = replace(workspace, backend=async_store_backend(workspace.backend))
        arguments["backend"] = workspace.backend
    context = AgentRunPreparation(
        identity, None if workspace is None else workspace.workspace
    )
    arguments["middleware"] = await _role_middleware(
        arguments,
        static_tools=static_tools,
        prepare_tools=prepare_tools,
        context=context,
        workspace=workspace,
    )
    subagents: list[dict[str, object]] = []
    for raw_spec in _subagent_specs(arguments.get("subagents")):
        spec = dict(raw_spec)
        if "runnable" not in spec and "graph_id" not in spec:
            spec["middleware"] = await _role_middleware(
                spec,
                static_tools=spec.get("tools", static_tools),
                prepare_tools=spec.pop("prepare_tools", None),
                context=context,
                workspace=workspace,
            )
        subagents.append(spec)
    if arguments.get("subagents") is not None:
        arguments["subagents"] = subagents
    return PreparedAgentArguments(arguments, workspace)
