"""Workspace, prepared tools, and context keep their types through build()."""

from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict, assert_type

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool

from tinkerfin import AgentRuntime, TinkerFin
from tinkerfin.runtime import TinkerFin as RuntimeBuilder
from tinkerfin.subagents import SubAgent
from tinkerfin_contracts import AgentRunPreparation, Workspace


class Context(TypedDict):
    customer: str


async def prepare_files(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
    assert_type(run.workspace, Path)
    assert_type(run.identity.namespace, str)
    return []


async def prepare_text(run: AgentRunPreparation[str]) -> Sequence[BaseTool]:
    assert_type(run.workspace, str)
    return []


async def prepare_without_workspace(
    run: AgentRunPreparation[None],
) -> Sequence[BaseTool]:
    assert_type(run.workspace, None)
    return []


def build(workspace: Workspace[Path, BackendProtocol]) -> None:
    reader: SubAgent[Path] = {
        "name": "reader",
        "description": "Read workspace reports",
        "system_prompt": "Read reports",
        "prepare_tools": prepare_files,
    }
    builder = TinkerFin().with_namespace("company")
    runtime = builder.build(
        model="provider:model",
        backend=workspace,
        prepare_tools=prepare_files,
        subagents=[reader],
        context_schema=Context,
    )
    assert_type(runtime, AgentRuntime[Context])
    runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": []},
        context={"customer": "customer"},
    )
    assert_type(
        builder.build(model="provider:model", prepare_tools=prepare_without_workspace),
        AgentRuntime[None],
    )
    source_runtime = (
        RuntimeBuilder()
        .with_namespace("company")
        .build(
            model="provider:model",
            backend=workspace,
            prepare_tools=prepare_files,
            subagents=[reader],
        )
    )
    assert_type(source_runtime, AgentRuntime[None])
