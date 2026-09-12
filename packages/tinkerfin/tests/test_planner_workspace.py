"""Planner reads respect workspace permissions without inheriting prepared tools."""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pytest
from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from test_plan_mode import _FakeModel, _parts, _planner
from test_runtime_workspace import _Workspace

from tinkerfin import TinkerFin
from tinkerfin_contracts import AgentRunPreparation


@pytest.mark.parametrize("mode", ["allow", "deny", "interrupt"])
@pytest.mark.parametrize("name", ["read_file", "ls", "glob", "grep"])
async def test_planner_workspace_filters_protected_reads(
    mode: Literal["allow", "deny", "interrupt"], name: str
) -> None:
    store = InMemoryStore()
    for filename in ("public", "secret"):
        await store.aput(
            ("dGVzdA", "planning"),
            f"/{filename}.txt",
            dict(create_file_data(f"{filename} information")),
        )
    workspace = _Workspace(
        CompositeBackend(
            default=StateBackend(),
            routes={"/memory/": StoreBackend(namespace=lambda _: ("planning",))},
        )
    )
    arguments: dict[str, str] = {
        "read_file": {"file_path": "/memory/secret.txt"},
        "ls": {"path": "/memory/"},
        "glob": {"pattern": "*.txt", "path": "/memory/"},
        "grep": {"pattern": "information", "path": "/memory/"},
    }[name]
    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": arguments, "id": "inspect"},
                    {
                        "name": "read_file",
                        "args": {"file_path": "/memory/public.txt"},
                        "id": "read-public",
                    },
                ],
            ),
            _planner(),
        ]
    )
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            backend=workspace,
            store=store,
            checkpointer=InMemorySaver(),
            permissions=[
                FilesystemPermission(["read"], ["/memory/public.txt"], "allow"),
                FilesystemPermission(["read"], ["/memory/**"], mode),
            ],
        )
    )
    parts = await _parts(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-permissions",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    replies: dict[str, str] = {}
    for part in parts:
        if part["type"] != "messages":
            continue
        payload = part["data"]
        if isinstance(payload, tuple) and isinstance(payload[0], ToolMessage):
            message = payload[0]
            replies[message.tool_call_id] = str(message.content)
    assert "public information" in replies["read-public"]
    protected = "secret information" if name == "read_file" else "secret.txt"
    assert (protected in replies["inspect"]) is (mode == "allow")
    assert workspace.closed == workspace.opened and len(workspace.opened) == 1


async def test_planner_does_not_inherit_prepared_main_tools_even_if_read_only() -> None:
    @tool
    async def read_customer() -> str:
        """Read customer information for execution."""
        raise AssertionError("The Planner must not receive prepared main tools")

    read_customer.metadata = {"read_only": True}
    workspace = _Workspace()
    prepared: list[Path] = []

    async def prepare(run: AgentRunPreparation[Path]) -> Sequence[BaseTool]:
        prepared.append(run.workspace)
        return [read_customer]

    model = _FakeModel(responses=[_planner()])
    runtime = (
        TinkerFin()
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            backend=workspace,
            prepare_tools=prepare,
            checkpointer=InMemorySaver(),
        )
    )
    await _parts(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="message")]},
        run_id="planner-prepared-tools",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert prepared == [Path("/files/test")]
    planner_tools = [set(names) for names in model.bound_tool_names]
    assert planner_tools and all(
        "read_customer" not in names for names in planner_tools
    )
    assert workspace.closed == workspace.opened
