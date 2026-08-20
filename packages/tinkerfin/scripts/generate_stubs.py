"""Generate explicit PyCharm-facing signatures from locked upstream source."""

from __future__ import annotations

import argparse
import ast
import copy
import inspect
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path

from deepagents.graph import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from tinkerfin.deep_agent import DeepAgentDefinition

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_PACKAGE = _PACKAGE_ROOT / "src/tinkerfin"
_DEEP_AGENT_STUB = _PACKAGE / "deep_agent.pyi"
_INIT_STUB = _PACKAGE / "__init__.pyi"


class _RenameTypes(ast.NodeTransformer):
    def __init__(self, replacements: Mapping[str, str]) -> None:
        self._replacements = replacements

    def visit_Name(self, node: ast.Name) -> ast.Name:
        return ast.copy_location(
            ast.Name(id=self._replacements.get(node.id, node.id), ctx=node.ctx),
            node,
        )


def _method(
    function: Callable[..., object],
    *,
    add_self: bool = False,
    replacements: Mapping[str, str] | None = None,
    return_type: str,
) -> str:
    module = ast.parse(textwrap.dedent(inspect.getsource(function)))
    source = next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    arguments = copy.deepcopy(source.args)
    if add_self:
        arguments.args.insert(0, ast.arg(arg="self"))
    placeholder = ast.parse("def generated(): ...").body[0]
    assert isinstance(placeholder, ast.FunctionDef)
    placeholder.name = source.name
    placeholder.args = arguments
    placeholder.decorator_list = []
    placeholder.returns = ast.parse(return_type, mode="eval").body
    placeholder.type_comment = None
    if replacements:
        placeholder = _RenameTypes(replacements).visit(placeholder)
    ast.fix_missing_locations(placeholder)
    return textwrap.indent(ast.unparse(placeholder), "    ")


def _format(content: str, *, target: Path) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(_REPOSITORY_ROOT / "pyproject.toml"),
            "--stdin-filename",
            target.name,
            "-",
        ],
        cwd=_REPOSITORY_ROOT,
        input=content,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _render_deep_agent_stub() -> str:
    astream_arguments = {"InputT": "InputAgentState"}
    content = f"""# ruff: noqa: F403, F405
# 此文件由 scripts/generate_stubs.py 根据锁定依赖生成，请勿手工维护参数列表
from collections.abc import Mapping, Sequence
from typing import Generic, Literal

from deepagents.graph import *
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.pregel.main import All, DeprecatedKwargs, Durability, RunControl, StreamMode
from langgraph.types import Command
from typing_extensions import Unpack

from tinkerfin_agui_adapter import Identity

from .agui_resume import AgUiResumeBinding
from .runtime import AgUiEventStream, EventObserver, NativeGraphRunStream, PartObserver

class DeepAgentRuntime(Generic[ContextT]):
{_method(CompiledStateGraph.astream, replacements=astream_arguments, return_type="NativeGraphRunStream")}

class DeepAgentAgUiRuntime(Generic[ContextT]):
{_method(CompiledStateGraph.astream, replacements=astream_arguments, return_type="AgUiEventStream")}

class DeepAgentDefinition(Generic[ContextT]):
{_method(DeepAgentDefinition.new, return_type="DeepAgentRuntime[ContextT]")}
{_method(DeepAgentDefinition.new_agui, return_type="DeepAgentAgUiRuntime[ContextT]")}

CREATE_DEEP_AGENT: object
"""
    return _format(content, target=_DEEP_AGENT_STUB)


def _render_init_stub() -> str:
    content = f"""# ruff: noqa: F403, F405
# 此文件由 scripts/generate_stubs.py 根据锁定依赖生成，请勿手工维护参数列表
from collections.abc import Callable, Sequence
from typing import Any

from deepagents.graph import *

from tinkerfin_agui_adapter import Identity as Identity

from .agui_resume import AgUiResumeBinding as AgUiResumeBinding
from .coordination import InMemoryRunCoordinator as InMemoryRunCoordinator
from .coordination import RunCoordinator as RunCoordinator
from .deep_agent import DeepAgentAgUiRuntime as DeepAgentAgUiRuntime
from .deep_agent import DeepAgentDefinition as DeepAgentDefinition
from .deep_agent import DeepAgentRuntime as DeepAgentRuntime
from .runtime import AgUiEventStream as AgUiEventStream
from .runtime import AgUiNativeStreamConfig as AgUiNativeStreamConfig
from .runtime import AgUiNativeStreamConfigurationError as AgUiNativeStreamConfigurationError
from .runtime import AgUiNativeStreamInvocation as AgUiNativeStreamInvocation
from .runtime import AgUiSettlementTimeoutError as AgUiSettlementTimeoutError
from .runtime import EventObserver as EventObserver
from .runtime import GraphRunStream as GraphRunStream
from .runtime import NativeGraphRunStream as NativeGraphRunStream
from .runtime import NativeStreamPart as NativeStreamPart
from .runtime import NativeTinkerFinRun as NativeTinkerFinRun
from .runtime import PartObserver as PartObserver
from .runtime import SseBody as SseBody
from .runtime import SseEventIdResolver as SseEventIdResolver
from .runtime import SseMapper as SseMapper
from .runtime import SsePayload as SsePayload
from .runtime import SsePreflight as SsePreflight
from .runtime import TinkerFin as _RuntimeTinkerFin
from .runtime import TinkerFinRun as TinkerFinRun

class TinkerFin(_RuntimeTinkerFin):
{_method(create_deep_agent, add_self=True, return_type="DeepAgentDefinition[ContextT]")}

__all__: list[str]
"""
    return _format(content, target=_INIT_STUB)


def render_stubs() -> dict[Path, str]:
    return {
        _DEEP_AGENT_STUB: _render_deep_agent_stub(),
        _INIT_STUB: _render_init_stub(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验已提交 stub，不写入文件",
    )
    options = parser.parse_args()
    rendered = render_stubs()
    if options.check:
        drift = [
            path
            for path, content in rendered.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if drift:
            for path in drift:
                print(f"generated stub drift: {path}", file=sys.stderr)
            return 1
        return 0
    for path, content in rendered.items():
        path.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
