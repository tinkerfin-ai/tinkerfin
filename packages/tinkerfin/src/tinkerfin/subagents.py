"""Declare delegated agents and their optional per-Run business tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Generic, NotRequired

from deepagents import SubAgent as DeepAgentsSubAgent
from langchain_core.tools import BaseTool
from typing_extensions import TypeVar

from tinkerfin_contracts import AgentRunPreparation

WorkspaceT = TypeVar("WorkspaceT", default=None)


class SubAgent(DeepAgentsSubAgent, Generic[WorkspaceT]):
    """Describe a delegated agent with tools prepared for its role each Run.

    The usual name, description, instructions, tools, model, middleware, skills,
    and permission fields follow the Deep Agents declaration. ``prepare_tools``
    adds an asynchronous function receiving the Run identity and its borrowed
    workspace. Its returned tools belong only to this agent. The workspace must
    remain owned by the provider; these tools cannot retain it beyond the Run.

    Use the workspace capability type as the generic argument when this agent
    prepares tools. Without a workspace, the preparation receives ``None``.
    Static tools retain ordinary inheritance from the main agent unless this
    declaration provides its own tools. Prepared business tools must use distinct
    names; built-in file, Todo, and task tool names are reserved in every role.
    """

    prepare_tools: NotRequired[
        Callable[[AgentRunPreparation[WorkspaceT]], Awaitable[Sequence[BaseTool]]]
    ]


__all__ = ["SubAgent"]
