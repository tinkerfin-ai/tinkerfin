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
from tinkerfin.runtime import TinkerFin as RuntimeTinkerFin

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_PACKAGE = _PACKAGE_ROOT / "src/tinkerfin"
_DEEP_AGENT_STUB = _PACKAGE / "deep_agent.pyi"
_INIT_STUB = _PACKAGE / "__init__.pyi"


class _RenameTypes(ast.NodeTransformer):
    def __init__(self, replacements: Mapping[str, str]) -> None:
        self._replacements = replacements

    def visit_Name(self, node: ast.Name) -> ast.expr:
        replacement = self._replacements.get(node.id)
        if replacement is None:
            return node
        return ast.copy_location(ast.parse(replacement, mode="eval").body, node)


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


def _with_pyright_ignores(
    content: str,
    ignores: Mapping[str, tuple[str, ...]],
) -> str:
    """Annotate locked upstream generic gaps without changing public signatures."""

    lines = content.splitlines()
    for index, line in enumerate(lines):
        for marker, rules in ignores.items():
            if marker in line:
                lines[index] = f"{line}  # pyright: ignore[{','.join(rules)}]"
                break
    return "\n".join(lines) + "\n"


def _render_deep_agent_stub() -> str:
    astream_arguments = {"InputT": "InputAgentState"}
    agui_astream = _method(
        CompiledStateGraph.astream,
        replacements=astream_arguments,
        return_type="AgUiEventStream",
    ).replace(
        "input: InputAgentState | Command | None,",
        "input: InputAgentState,",
    )
    content = f"""# ruff: noqa: F403, F405
# Generated from locked dependencies by scripts/generate_stubs.py; do not edit signatures manually.
from collections.abc import Mapping, Sequence
from typing import Generic, Literal, overload

from deepagents.graph import *
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.pregel.main import All, DeprecatedKwargs, Durability, RunControl, StreamMode
from langgraph.types import Command
from typing_extensions import Unpack

from tinkerfin_agui_adapter import Identity

from .agui_resume import AgUiResumeBinding, AgUiResumeCheckpointObserver
from .plan import AgentMode
from .runtime import AgUiEventStream, EventObserver, NativeGraphRunStream, PartObserver

class DeepAgentRuntime(Generic[ContextT]):
{_method(CompiledStateGraph.astream, replacements=astream_arguments, return_type="NativeGraphRunStream")}

class DeepAgentAgUiRuntime(Generic[ContextT]):
{agui_astream}

class DeepAgentAgUiResumeRuntime(Generic[ContextT]):
    def astream(
        self,
        *,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Literal["sync"] | None = None,
        control: RunControl | None = None,
        subgraphs: bool = False,
        debug: bool | None = None,
        version: Literal["v1", "v2"] = "v1",
        **kwargs: Unpack[DeprecatedKwargs],
    ) -> AgUiEventStream: ...

class DeepAgentDefinition(Generic[ContextT]):
{_method(DeepAgentDefinition.new, return_type="DeepAgentRuntime[ContextT]")}
    @overload
    def new_agui(
        self,
        *,
        identity: Identity,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: None = None,
        on_resume_checkpointed: None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[ContextT]: ...
    @overload
    def new_agui(
        self,
        *,
        identity: Identity,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: AgUiResumeBinding,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiResumeRuntime[ContextT]: ...

CREATE_DEEP_AGENT: object
"""
    formatted = _format(content, target=_DEEP_AGENT_STUB)
    return _with_pyright_ignores(
        formatted,
        {
            "input: InputAgentState | Command | None,": (
                "reportMissingTypeArgument",
                "reportUnknownParameterType",
            ),
        },
    )


def _render_init_stub() -> str:
    content = f"""# ruff: noqa: F403, F405
# Generated from locked dependencies by scripts/generate_stubs.py; do not edit signatures manually.
from collections.abc import Callable, Sequence
from typing import Any

from deepagents.graph import *

from tinkerfin_agui_adapter import Identity as Identity

from ._hitl import TINKERFIN_HITL_CONTRACT as TINKERFIN_HITL_CONTRACT
from ._tasks import join_task as join_task
from .agui_resume import AgUiResumeBinding as AgUiResumeBinding
from .agui_resume import AgUiResumeCheckpoint as AgUiResumeCheckpoint
from .agui_resume import AgUiResumeCheckpointObserver as AgUiResumeCheckpointObserver
from .coordination import InMemoryRunCoordinator as InMemoryRunCoordinator
from .coordination import RunCoordinator as RunCoordinator
from .deep_agent import DeepAgentAgUiResumeRuntime as DeepAgentAgUiResumeRuntime
from .deep_agent import DeepAgentAgUiRuntime as DeepAgentAgUiRuntime
from .deep_agent import DeepAgentDefinition as DeepAgentDefinition
from .deep_agent import DeepAgentRuntime as DeepAgentRuntime
from .errors import AgUiNativeStreamConfigurationError as AgUiNativeStreamConfigurationError
from .errors import AgUiResumeBindingError as AgUiResumeBindingError
from .errors import RedisLeaseError as RedisLeaseError
from .errors import RedisLeaseLifecycleError as RedisLeaseLifecycleError
from .errors import RedisLeaseProtocolError as RedisLeaseProtocolError
from .errors import RedisLeaseTimeoutError as RedisLeaseTimeoutError
from .errors import RedisLeaseUnavailableError as RedisLeaseUnavailableError
from .errors import RunCoordinationError as RunCoordinationError
from .errors import RunCoordinationOwnershipLostError as RunCoordinationOwnershipLostError
from .errors import RunCoordinationTimeoutError as RunCoordinationTimeoutError
from .errors import RunCoordinationUnavailableError as RunCoordinationUnavailableError
from .errors import TinkerFinError as TinkerFinError
from .errors import TinkerFinErrorCode as TinkerFinErrorCode
from .errors import TinkerFinLifecycleError as TinkerFinLifecycleError
from .errors import TinkerFinStreamProtocolError as TinkerFinStreamProtocolError
from .plan import AgentMode as AgentMode
from .plan import ClarificationFormBase as _ClarificationFormBase
from .plan import DefaultClarificationForm as _DefaultClarificationForm
from .plan import PlanContentModel as _PlanContentModel
from .plan import PlanReviewAction as _PlanReviewAction
from .plan import StructuredPlanContent as _StructuredPlanContent
from .plan._config import DEFAULT_PLAN_REVIEW_ACTIONS as _DEFAULT_PLAN_REVIEW_ACTIONS
from .runtime import AgUiEventStream as AgUiEventStream
from .runtime import AgUiSettlementTimeoutError as AgUiSettlementTimeoutError
from .runtime import EventObserver as EventObserver
from .runtime import NativeGraphRunStream as NativeGraphRunStream
from .runtime import NativeStreamPart as NativeStreamPart
from .runtime import PartObserver as PartObserver
from .runtime import SseBody as SseBody
from .runtime import SseEventIdResolver as SseEventIdResolver
from .runtime import SseMapper as SseMapper
from .runtime import SsePayload as SsePayload
from .runtime import SsePreflight as SsePreflight
from .runtime import TinkerFin as _RuntimeTinkerFin

class TinkerFin(_RuntimeTinkerFin):
{
        _method(
            RuntimeTinkerFin.plan,
            replacements={
                "ClarificationFormBase": "_ClarificationFormBase",
                "DefaultClarificationForm": "_DefaultClarificationForm",
                "PlanContentModel": "_PlanContentModel",
                "PlanReviewAction": "_PlanReviewAction",
                "StructuredPlanContent": "_StructuredPlanContent",
                "DEFAULT_PLAN_REVIEW_ACTIONS": "_DEFAULT_PLAN_REVIEW_ACTIONS",
            },
            return_type="TinkerFin",
        )
    }
{_method(create_deep_agent, add_self=True, return_type="DeepAgentDefinition[ContextT]")}

__all__: list[str]
"""
    formatted = _format(content, target=_INIT_STUB)
    return _with_pyright_ignores(
        formatted,
        {
            "tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,": (
                "reportMissingTypeArgument",
                "reportUnknownParameterType",
            ),
            "checkpointer: Checkpointer | None = None,": (
                "reportUnknownParameterType",
            ),
            "cache: BaseCache | None = None,": (
                "reportMissingTypeArgument",
                "reportUnknownParameterType",
            ),
        },
    )


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
