"""Run-scoped workspace borrowing and role-local business tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore
from pydantic import Field
from test_runtime_store import _Model

from tinkerfin import TinkerFin
from tinkerfin.deep_agent import create_graph
from tinkerfin_contracts import AgentRunPreparation, PreparedWorkspace, RunIdentity
from tinkerfin_messaging import MemoryBackend, Messaging


class _Workspace:
    def __init__(self, backend: BackendProtocol | None = None) -> None:
        self.backend = StateBackend() if backend is None else backend
        self.opened: list[RunIdentity] = []
        self.closed: list[RunIdentity] = []
        self.context = ContextVar("test_workspace", default="outside")

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[Path, BackendProtocol]]:
        self.opened.append(identity)
        token = self.context.set(identity.namespace)
        try:
            yield PreparedWorkspace(
                Path("/files") / identity.namespace,
                self.backend,
                filesystem_instructions="File paths are relative to the selected workspace.",
            )
        finally:
            self.context.reset(token)
            self.closed.append(identity)


class _RecordingModel(_Model):
    seen: list[set[str]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        self.seen.append({tool.name for tool in tools if isinstance(tool, BaseTool)})
        return super().bind_tools(tools, **kwargs)


async def test_prepared_tools_allow_middleware_without_registered_tools() -> None:
    workspace = _Workspace()

    @tool
    async def inspect_workspace() -> str:
        """Inspect files in the prepared workspace."""
        return "ready"

    async def prepare_tools(_run: AgentRunPreparation[Path]) -> list[BaseTool]:
        return [inspect_workspace]

    model = _RecordingModel(responses=[AIMessage(content="ready")])
    runtime = (
        TinkerFin()
        .with_namespace("user-a")
        .build(
            model=model,
            backend=workspace,
            prepare_tools=prepare_tools,
            middleware=[PatchToolCallsMiddleware()],
        )
    )
    stream = runtime.open_run(
        thread_id="conversation",
        run_id="run",
        input={"messages": [{"role": "user", "content": "Hi"}]},
    )
    try:
        assert [part async for part in stream]
    finally:
        await stream.aclose()
    assert model.seen and "inspect_workspace" in model.seen[0]
    assert (
        workspace.opened
        == workspace.closed
        == [runtime.run_identity("conversation", "run")]
    )


def _delegate(name: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "id": call_id,
                "args": {
                    "description": "Inspect available files",
                    "subagent_type": name,
                },
                "type": "tool_call",
            }
        ],
    )


async def test_prepared_tools_are_role_local_while_static_tools_are_inherited() -> None:
    workspace = _Workspace()
    preparations: list[tuple[str, Path]] = []

    @tool
    async def shared_tool() -> str:
        """Read common reference information."""
        return "shared"

    @tool
    async def main_tool() -> str:
        """Read the main agent's business information."""
        return "main"

    @tool
    async def reader_tool() -> str:
        """Read information available to the delegated reader."""
        return "reader"

    async def prepare_main(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
        preparations.append(("main", run.workspace))
        assert workspace.context.get() == run.identity.namespace
        return [main_tool]

    async def prepare_reader(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
        preparations.append(("reader", run.workspace))
        return [reader_tool]

    parent = _RecordingModel(
        responses=[
            _delegate("general-purpose", "gp"),
            AIMessage(content="gp done"),
            _delegate("reader", "reader"),
            AIMessage(content="done"),
        ]
    )
    child = _RecordingModel(responses=[AIMessage(content="reader done")])
    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=parent,
            tools=[shared_tool],
            backend=workspace,
            prepare_tools=prepare_main,
            subagents=[
                {
                    "name": "reader",
                    "description": "Read files",
                    "system_prompt": "Read files",
                    "model": child,
                    "prepare_tools": prepare_reader,
                }
            ],
        )
    )
    assert workspace.opened == preparations == []
    await runtime.ainvoke(thread_id="thread", run_id="run", input={"messages": []})
    assert preparations == [
        ("main", Path("/files/company")),
        ("reader", Path("/files/company")),
    ]
    assert len(parent.seen) == 4 and len(child.seen) == 1
    assert "main_tool" in parent.seen[0] and "main_tool" not in parent.seen[1]
    assert all("shared_tool" in names for names in [*parent.seen, *child.seen])
    assert all("reader_tool" not in names for names in parent.seen)
    assert "reader_tool" in child.seen[0] and "main_tool" not in child.seen[0]
    assert workspace.closed == workspace.opened


@pytest.mark.parametrize("duplicate", ["static", "prepared"])
async def test_duplicate_tool_names_fail_and_release_the_workspace(
    duplicate: str,
) -> None:
    workspace = _Workspace()

    @tool
    async def read_report() -> str:
        """Read a report."""
        return "report"

    async def prepare(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
        del run
        return [read_report] * (2 if duplicate == "prepared" else 1)

    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            tools=[read_report] if duplicate == "static" else [],
            backend=workspace,
            prepare_tools=prepare,
        )
    )
    with pytest.raises(ValueError, match="duplicate tool name"):
        await runtime.ainvoke(thread_id="thread", run_id="run", input={"messages": []})
    assert len(workspace.opened) == 1 and workspace.closed == workspace.opened


@pytest.mark.parametrize(
    "name", ["read_file", "write_todos", "task", "start_async_task"]
)
@pytest.mark.parametrize("use_workspace", [False, True])
@pytest.mark.parametrize("child", [False, True])
async def test_prepared_business_tools_cannot_replace_builtin_capabilities(
    name: str, use_workspace: bool, child: bool
) -> None:
    @tool(name)
    async def business_tool() -> str:
        """Read business information."""
        return "business"

    async def prepare(run: AgentRunPreparation[object]) -> Sequence[BaseTool]:
        return [business_tool]

    workspace = _Workspace() if use_workspace else None
    model = _RecordingModel(responses=[AIMessage(content="done")])
    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=model,
            backend=workspace,
            prepare_tools=None if child else prepare,
            subagents=[
                {
                    "name": "reader",
                    "description": "Read",
                    "system_prompt": "Read",
                    "prepare_tools": prepare,
                }
            ]
            if child
            else None,
        )
    )
    with pytest.raises(ValueError, match=f"reserved tool name: {name}"):
        await runtime.ainvoke(thread_id="thread", run_id="run", input={"messages": []})
    assert model.seen == []
    if workspace is not None:
        assert len(workspace.opened) == 1 and workspace.closed == workspace.opened


@pytest.mark.parametrize("protocol", ["native", "agui"])
async def test_cancelled_tool_preparation_releases_workspace_without_building(
    protocol: str,
) -> None:
    workspace = _Workspace()
    entered = asyncio.Event()
    model = _RecordingModel(responses=[AIMessage(content="done")])

    async def prepare(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
        del run
        entered.set()
        await asyncio.Event().wait()
        return []

    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=model,
            backend=workspace,
            prepare_tools=prepare,
        )
    )
    stream = (runtime.open_run if protocol == "native" else runtime.open_agui_run)(
        thread_id="thread", run_id="run", input={"messages": []}
    )
    preparing = asyncio.create_task(stream.messaging_owner_preflight())
    await entered.wait()
    await stream.aclose()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert model.seen == []
    assert len(workspace.opened) == 1 and workspace.closed == workspace.opened


async def test_direct_graph_rejects_lazy_workspace_and_identity_tools_before_io() -> (
    None
):
    workspace = _Workspace()
    prepared: list[RunIdentity] = []

    async def prepare(run: AgentRunPreparation[None]) -> Sequence[BaseTool]:
        prepared.append(run.identity)
        return []

    builder = TinkerFin().with_namespace("company")
    model = _Model(responses=[AIMessage(content="done")])
    for runtime in [
        builder.build(model=model, backend=workspace),
        builder.build(model=model, prepare_tools=prepare),
    ]:
        with pytest.raises(ValueError, match="managed Runtime"):
            await create_graph(runtime)
    assert workspace.opened == prepared == []


async def test_messaging_replay_does_not_open_workspace_or_prepare_tools() -> None:
    workspace = _Workspace()
    prepared: list[RunIdentity] = []

    async def prepare(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
        prepared.append(run.identity)
        return []

    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=workspace,
            prepare_tools=prepare,
        )
    )
    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="workspace-runs")
        for _ in range(2):
            body = await channel.open_sse(
                runtime.open_agui_run(
                    thread_id="thread", run_id="run", input={"messages": []}
                ),
                after=0,
            )
            assert [frame async for frame in body]
    assert workspace.closed == workspace.opened == prepared
    assert len(prepared) == 1


async def test_prepared_composite_backend_cannot_bypass_runtime_store_isolation() -> (
    None
):
    workspace = _Workspace(
        CompositeBackend(
            default=StateBackend(),
            routes={
                "/memory/": StoreBackend(
                    store=InMemoryStore(), namespace=lambda _: ("memory",)
                ),
            },
        )
    )
    runtime = (
        TinkerFin()
        .with_namespace("company")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            backend=workspace,
        )
    )
    with pytest.raises(ValueError, match="StoreBackend must use the Runtime store"):
        await runtime.ainvoke(thread_id="thread", run_id="run", input={"messages": []})
    assert len(workspace.opened) == 1 and workspace.closed == workspace.opened
