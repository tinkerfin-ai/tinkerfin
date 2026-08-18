"""Model-visible path and HITL execution contracts for rooted OpenSandbox."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StoreBackend
from deepagents.backends.protocol import (
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import Field

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxHandle,
    RootedOpenSandboxBackend,
    build_rooted_filesystem_middleware,
)


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """Allow deterministic responses to invoke Deep Agents tools."""

    bound_execute_descriptions: list[str] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tool_choice, kwargs
        execute_tool = next(
            (
                tool
                for tool in tools
                if isinstance(tool, BaseTool) and tool.name == "execute"
            ),
            None,
        )
        if execute_tool is not None:
            self.bound_execute_descriptions.append(execute_tool.description)
        return self


class _LocalOpenSandboxBackend(LocalShellBackend):
    """Model OpenSandbox file and Shell cwd semantics in a temporary directory."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(
            root_dir=workspace,
            virtual_mode=False,
            inherit_env=True,
        )
        self.executed_commands: list[str] = []

    @contextmanager
    def _rooted_file_operation(self) -> Iterator[None]:
        """Provide the internal file-operation scope required by the rooted view."""
        yield

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Record commands while retaining LocalShellBackend execution behavior."""
        self.executed_commands.append(command)
        return super().execute(command, timeout=timeout)

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Model OpenSandboxBackend offload while retaining real command execution."""
        del capture_path, max_inline_bytes, max_capture_bytes
        return ExecuteOffloadResult(
            offloaded=False,
            response=self.execute(command, timeout=timeout),
        )

    async def aexecute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Provide the native async offload entry used by OpenSandboxBackend."""
        del capture_path, max_inline_bytes, max_capture_bytes
        return ExecuteOffloadResult(
            offloaded=False,
            response=await self.aexecute(command, timeout=timeout),
        )

    async def _aupload_rooted_file(
        self,
        *,
        root: str,
        path: str,
        content: bytes,
    ) -> FileUploadResponse:
        """Model the concrete descriptor-upload result for middleware tests."""
        physical = str(PurePosixPath(root, path.lstrip("/")))
        response = (await self.aupload_files([(physical, content)]))[0]
        return FileUploadResponse(path=path, error=response.error)

    async def _adownload_rooted_file(
        self,
        *,
        root: str,
        path: str,
    ) -> FileDownloadResponse:
        """Model the concrete descriptor-download result for middleware tests."""
        physical = str(PurePosixPath(root, path.lstrip("/")))
        response = (await self.adownload_files([physical]))[0]
        return FileDownloadResponse(
            path=path,
            content=response.content,
            error=response.error,
        )

    async def _aexecute_rooted_offload(
        self,
        *,
        root: str,
        command: str,
        capture_path: str,
        max_inline_bytes: int,
        max_capture_bytes: int | None,
        timeout: int | None,
    ) -> ExecuteOffloadResult:
        """Model the concrete rooted offload boundary for middleware tests."""
        physical = str(PurePosixPath(root, capture_path.lstrip("/")))
        return await self.aexecute_with_offload(
            command,
            physical,
            max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
            timeout=timeout,
        )


def _rooted_backends(
    tmp_path: Path,
) -> tuple[Path, _LocalOpenSandboxBackend, RootedOpenSandboxBackend, CompositeBackend]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    physical_backend = _LocalOpenSandboxBackend(workspace)
    rooted_backend = RootedOpenSandboxBackend(
        OpenSandboxHandle(cast(OpenSandboxBackend, physical_backend)),
        root=str(workspace),
    )
    composite_backend = CompositeBackend(
        default=rooted_backend,
        routes={
            "/memories/": StoreBackend(
                namespace=lambda _runtime: ("tests", "memories")
            ),
            "/policies/": StoreBackend(
                namespace=lambda _runtime: ("tests", "policies")
            ),
        },
    )
    return workspace, physical_backend, rooted_backend, composite_backend


def _build_rooted_middleware(
    backend: CompositeBackend,
    *,
    permissions: Sequence[FilesystemPermission] | None = None,
):
    return build_rooted_filesystem_middleware(
        backend,
        permissions=permissions,
    )


def test_rooted_middleware_exposes_distinct_file_and_shell_path_contract(
    tmp_path: Path,
) -> None:
    """Keep virtual absolute file paths distinct from Shell paths in model requests."""

    _, _, _, backend = _rooted_backends(tmp_path)
    middleware = _build_rooted_middleware(backend)
    model = _ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    request = ModelRequest(
        model=model,
        messages=[HumanMessage(content="创建并运行脚本")],
        system_message=SystemMessage(content="base prompt"),
        tools=list(middleware.tools),
        state=cast(Any, {"messages": []}),
    )
    captured_request: ModelRequest[Any] | None = None

    def capture(modified_request: ModelRequest[Any]) -> ModelResponse[Any]:
        nonlocal captured_request
        captured_request = modified_request
        return ModelResponse(result=[AIMessage(content="captured")])

    middleware.wrap_model_call(request, capture)

    assert captured_request is not None
    tools_by_name = {tool.name: tool for tool in captured_request.tools}
    assert set(tools_by_name) == {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
    }
    execute_description = tools_by_name["execute"].description
    assert "python3 scripts/helloworld.py" in execute_description
    assert "Use absolute paths and avoid `cd`" not in execute_description
    assert "real container paths" in execute_description

    assert captured_request.system_message is not None
    system_prompt = captured_request.system_message.text
    assert "/scripts/helloworld.py" in system_prompt
    assert "scripts/helloworld.py" in system_prompt
    assert "/home/user" in system_prompt
    assert "/memories/" in system_prompt
    assert "not accessible from the shell" in system_prompt


@pytest.mark.asyncio
async def test_rooted_permission_denies_route_write(tmp_path: Path) -> None:
    """Keep route-scoped write denials after rooted middleware replacement."""

    _, _, _, backend = _rooted_backends(tmp_path)
    store = InMemoryStore()
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/private/**"],
            mode="deny",
        )
    ]
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/private/rule.md",
                            "content": "blocked\n",
                        },
                        "id": "call-write-private-policy",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="write attempted"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=[
            _build_rooted_middleware(
                backend,
                permissions=permissions,
            )
        ],
        permissions=permissions,
        store=store,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "写入私有策略"}]}
    )

    write_result = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "write_file"
    )
    assert write_result.status == "error"
    assert "permission denied" in str(write_result.content).lower()
    assert (
        await store.aget(
            ("tests", "policies"),
            "/private/rule.md",
        )
        is None
    )


@pytest.mark.asyncio
async def test_rooted_permission_allows_first_matching_route_write(
    tmp_path: Path,
) -> None:
    """Preserve first-match allow rules ahead of a broader route denial."""

    _, _, _, backend = _rooted_backends(tmp_path)
    store = InMemoryStore()
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/public/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/**"],
            mode="deny",
        ),
    ]
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/public/rule.md",
                            "content": "allowed\n",
                        },
                        "id": "call-write-public-policy",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/private/second-rule.md",
                            "content": "blocked\n",
                        },
                        "id": "call-write-private-policy-after-allow",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="write completed"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=[
            _build_rooted_middleware(
                backend,
                permissions=permissions,
            )
        ],
        permissions=permissions,
        store=store,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "写入公开策略"}]}
    )

    public_write = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id == "call-write-public-policy"
    )
    private_write = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id == "call-write-private-policy-after-allow"
    )
    assert public_write.status == "success"
    assert private_write.status == "error"
    assert "permission denied" in str(private_write.content).lower()
    assert (
        await store.aget(
            ("tests", "policies"),
            "/public/rule.md",
        )
        is not None
    )
    assert (
        await store.aget(
            ("tests", "policies"),
            "/private/second-rule.md",
        )
        is None
    )


@pytest.mark.asyncio
async def test_rooted_permission_interrupts_route_write_until_approved(
    tmp_path: Path,
) -> None:
    """Pause a rooted route write until its permission decision is approved."""

    _, _, _, backend = _rooted_backends(tmp_path)
    store = InMemoryStore()
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/review/**"],
            mode="interrupt",
        )
    ]
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/review/rule.md",
                            "content": "approved\n",
                        },
                        "id": "call-write-review-policy",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="write approved"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=[
            _build_rooted_middleware(
                backend,
                permissions=permissions,
            )
        ],
        permissions=permissions,
        checkpointer=MemorySaver(),
        store=store,
    )
    config: RunnableConfig = {
        "configurable": {"thread_id": "rooted-route-permission-interrupt"}
    }

    interrupted = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "写入待审策略"}]},
        config=config,
    )

    assert interrupted.get("__interrupt__")
    assert not any(
        isinstance(message, ToolMessage) and message.name == "write_file"
        for message in interrupted["messages"]
    )
    assert (
        await store.aget(
            ("tests", "policies"),
            "/review/rule.md",
        )
        is None
    )

    resumed = await agent.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
    )

    write_result = next(
        message
        for message in resumed["messages"]
        if isinstance(message, ToolMessage) and message.name == "write_file"
    )
    assert write_result.status == "success"
    assert (
        await store.aget(
            ("tests", "policies"),
            "/review/rule.md",
        )
        is not None
    )


def test_rooted_permissions_reject_executable_default_backend_paths(
    tmp_path: Path,
) -> None:
    """Reject file-tool rules that Shell execution could bypass."""

    _, _, _, backend = _rooted_backends(tmp_path)
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/scripts/**"],
            mode="deny",
        )
    ]

    with pytest.raises(NotImplementedError):
        _build_rooted_middleware(
            backend,
            permissions=permissions,
        )


async def test_rooted_middleware_preserves_write_approval_and_relative_execution(
    tmp_path: Path,
) -> None:
    """Execute the approved virtual write through the matching relative Shell path."""

    workspace, physical_backend, rooted_backend, composite_backend = _rooted_backends(
        tmp_path
    )
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/scripts/helloworld.py",
                            "content": "print('hello from workspace')\n",
                        },
                        "id": "call-write-hello",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute",
                        "args": {"command": "python3 scripts/helloworld.py"},
                        "id": "call-run-hello",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="脚本已创建并成功运行"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=composite_backend,
        middleware=[_build_rooted_middleware(composite_backend)],
        interrupt_on={"write_file": True},
        checkpointer=MemorySaver(),
    )
    config: RunnableConfig = {
        "configurable": {"thread_id": "rooted-hitl-relative-shell"}
    }

    interrupted = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "创建并运行脚本"}]},
        config=config,
    )

    assert interrupted.get("__interrupt__")
    assert not (workspace / "scripts" / "helloworld.py").exists()

    resumed = await agent.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
    )

    assert (workspace / "scripts" / "helloworld.py").read_text(
        encoding="utf-8"
    ) == "print('hello from workspace')\n"
    execute_result = next(
        message
        for message in resumed["messages"]
        if isinstance(message, ToolMessage) and message.name == "execute"
    )
    assert "hello from workspace" in str(execute_result.content)
    assert "python3 scripts/helloworld.py" in physical_backend.executed_commands
    assert rooted_backend.id == physical_backend.id


@pytest.mark.asyncio
async def test_default_general_purpose_subagent_inherits_rooted_path_contract(
    tmp_path: Path,
) -> None:
    """Keep rooted Shell guidance in the default general-purpose subagent."""

    _, _, _, composite_backend = _rooted_backends(tmp_path)
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "确认工作区路径规则",
                            "subagent_type": "general-purpose",
                        },
                        "id": "call-general-purpose",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="子 Agent 已确认路径规则"),
            AIMessage(content="主 Agent 已收到结果"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=composite_backend,
        middleware=[_build_rooted_middleware(composite_backend)],
        store=InMemoryStore(),
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "委派检查路径规则"}]}
    )

    assert result["messages"][-1].text == "主 Agent 已收到结果"
    assert len(model.bound_execute_descriptions) >= 2
    assert all(
        "python3 scripts/helloworld.py" in description
        and "Use absolute paths and avoid `cd`" not in description
        for description in model.bound_execute_descriptions
    )


@pytest.mark.asyncio
async def test_default_general_purpose_subagent_inherits_rooted_permissions(
    tmp_path: Path,
) -> None:
    """Prevent the default subagent from bypassing a rooted route denial."""

    _, _, _, backend = _rooted_backends(tmp_path)
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/policies/private/**"],
            mode="deny",
        )
    ]
    store = InMemoryStore()
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "尝试写入受保护策略",
                            "subagent_type": "general-purpose",
                        },
                        "id": "call-protected-policy-subagent",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/policies/private/subagent-rule.md",
                            "content": "blocked\n",
                        },
                        "id": "call-subagent-write-private-policy",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="subagent write attempted"),
            AIMessage(content="main agent received result"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=[
            _build_rooted_middleware(
                backend,
                permissions=permissions,
            )
        ],
        permissions=permissions,
        store=store,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "委派策略写入"}]}
    )

    assert result["messages"][-1].text == "main agent received result"
    assert (
        await store.aget(
            ("tests", "policies"),
            "/private/subagent-rule.md",
        )
        is None
    )
