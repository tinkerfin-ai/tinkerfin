"""Generate public builder and execution signatures from current and locked source."""

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

from tinkerfin.runtime import TinkerFin

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_PACKAGE = _PACKAGE_ROOT / "src/tinkerfin"
_INIT_STUB = _PACKAGE / "__init__.pyi"
_BUILD_STUB = _PACKAGE / "_build_api.py"


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
    is_async: bool = False,
    docstring: str | None = None,
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
    # Generated stream attributes are synchronous factories even when their upstream
    # signature comes from an async-generator function. Explicit coroutine methods
    # opt in so the stub preserves their required ``await`` boundary.
    placeholder = ast.parse(
        "async def generated(): ..." if is_async else "def generated(): ..."
    ).body[0]
    assert isinstance(placeholder, (ast.FunctionDef, ast.AsyncFunctionDef))
    placeholder.name = source.name
    placeholder.args = arguments
    placeholder.decorator_list = []
    placeholder.returns = ast.parse(return_type, mode="eval").body
    placeholder.type_comment = None
    resolved_docstring = docstring or ast.get_docstring(source)
    if resolved_docstring is not None:
        placeholder.body.insert(
            0,
            ast.Expr(value=ast.Constant(value=resolved_docstring)),
        )
    if replacements:
        placeholder = _RenameTypes(replacements).visit(placeholder)
    ast.fix_missing_locations(placeholder)
    return textwrap.indent(ast.unparse(placeholder), "    ")


def _format(content: str, *, target: Path) -> str:
    linted = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(_REPOSITORY_ROOT / "pyproject.toml"),
            "--select",
            "I001,F401",
            "--fix",
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
    content = linted.stdout
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


_IMPORTS = "from collections.abc import Awaitable, Callable, Mapping, Sequence\nfrom typing import Any, Generic, Literal, overload\n\nfrom deepagents import (\n    AsyncSubAgent,\n    CompiledSubAgent,\n    DeepAgentState,\n    FilesystemPermission,\n)\nfrom .subagents import SubAgent, WorkspaceT\nfrom tinkerfin_contracts import Workspace, AgentRunPreparation\nfrom deepagents.backends import BackendProtocol\nfrom langchain.agents.middleware import AgentMiddleware, InterruptOnConfig\nfrom langchain.agents.middleware.types import InputAgentState, ResponseT, StateT_co\nfrom langchain.agents.structured_output import ResponseFormat\nfrom langchain_core.language_models import BaseChatModel\nfrom langchain_core.runnables import RunnableConfig\nfrom ag_ui.core import UserMessage\nfrom langgraph.pregel.main import All, Durability, RunControl, StreamMode\nfrom langchain_core.messages import SystemMessage\nfrom langchain_core.tools import BaseTool\nfrom langgraph.cache.base import BaseCache\nfrom langgraph.checkpoint.base import BaseCheckpointSaver\nfrom langgraph.store.base import BaseStore\nfrom langgraph.types import Checkpointer, Command\nfrom langgraph.typing import ContextT\n\nfrom tinkerfin_contracts import ContextKind as ContextKind\nfrom tinkerfin_contracts import RunIdentity as RunIdentity\nfrom tinkerfin_contracts import RuntimeObserver as _RuntimeObserver\n\nfrom ._call_observation import TraceContribution as TraceContribution\nfrom ._call_observation import trace_contribution as trace_contribution\nfrom .agui_input import AgUiUserInput as AgUiUserInput\nfrom .agui_resume import AgUiResumeBinding as AgUiResumeBinding\nfrom .agui_resume import AgUiResumeCheckpoint as AgUiResumeCheckpoint\nfrom .agui_resume import AgUiResumeCheckpointObserver as AgUiResumeCheckpointObserver\nfrom .agui_resume import AgUiResumeNotSavedObserver as AgUiResumeNotSavedObserver\nfrom .agui_resume import AgUiResumeRequest as AgUiResumeRequest\nfrom .errors import AgUiResumeBindingError as AgUiResumeBindingError\nfrom .errors import RunObservationError as RunObservationError\nfrom .errors import TinkerFinError as TinkerFinError\nfrom .errors import TinkerFinErrorCode as TinkerFinErrorCode\nfrom .errors import TinkerFinLifecycleError as TinkerFinLifecycleError\nfrom .errors import TinkerFinStreamProtocolError as TinkerFinStreamProtocolError\nfrom .media import AttachmentImage as AttachmentImage\nfrom .media import AttachmentSupport as AttachmentSupport\nfrom .plan import AgentMode as AgentMode\nfrom .plan import ClarificationFormBase as _ClarificationFormBase\nfrom .plan import ClarificationType as _ClarificationType\nfrom .plan import DefaultClarificationForm as _DefaultClarificationForm\nfrom .plan import PlanContentModel as _PlanContentModel\nfrom .plan import PlanReviewAction as _PlanReviewAction\nfrom .plan import StructuredPlanContent as _StructuredPlanContent\nfrom .plan._config import DEFAULT_ALLOWED_REVIEW_ACTIONS as _DEFAULT_ALLOWED_REVIEW_ACTIONS\nfrom .runtime import AgUiSettlementTimeoutError as AgUiSettlementTimeoutError\nfrom .runtime import EventObserver as EventObserver\nfrom .runtime import NativeStreamPart as NativeStreamPart\nfrom .runtime import PartObserver as PartObserver\nfrom .runtime import SseBody as SseBody\nfrom .runtime import SseEventIdResolver as SseEventIdResolver\nfrom .runtime import SseMapper as SseMapper\nfrom .runtime import SsePayload as SsePayload\nfrom .runtime import SsePreflight as SsePreflight\nfrom .runtime import TinkerFin as _RuntimeTinkerFin\nfrom ._lazy_run import AgUiRunStream as AgUiRunStream\nfrom ._lazy_run import NativeRunStream as NativeRunStream\nfrom ._terminal_observer import TerminalObserver as _TerminalObserver\nfrom .runtime import AgentRuntime as AgentRuntime\n"


def _function(source: Callable[..., object]) -> ast.FunctionDef | ast.AsyncFunctionDef:
    node = ast.parse(textwrap.dedent(inspect.getsource(source))).body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return node


def _declaration(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, docstring: str | None = None
) -> str:
    node.decorator_list = [ast.Name(id="overload", ctx=ast.Load())]
    node.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
    if docstring is not None:
        node.body.insert(0, ast.Expr(value=ast.Constant(value=docstring)))
    ast.fix_missing_locations(node)
    return textwrap.indent(ast.unparse(node), "    ")


def _build_overloads(*, method_name: str = "build") -> str:
    declarations: list[str] = []
    for workspace, typed in (
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ):
        node = _function(create_deep_agent)
        # The dependency leaves these generic values unspecified. Spell out its
        # dynamic boundary instead of leaking Unknown into strict callers.
        boundary_types = {
            "tools": "Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None",
            "checkpointer": "BaseCheckpointSaver[Any] | bool | None",
            "cache": "BaseCache[Any] | None",
        }
        for argument in (*node.args.args, *node.args.kwonlyargs):
            if argument.arg in boundary_types:
                argument.annotation = ast.parse(
                    boundary_types[argument.arg], mode="eval"
                ).body
        node.name = method_name
        node.args.args.insert(0, ast.arg(arg="self"))
        index = next(
            i
            for i, arg in enumerate(node.args.kwonlyargs)
            if arg.arg == "context_schema"
        )
        node.args.kwonlyargs[index].annotation = ast.parse(
            "type[ContextT]" if typed else "None", mode="eval"
        ).body
        node.args.kw_defaults[index] = None if typed else ast.Constant(value=None)
        backend_index = next(
            i for i, arg in enumerate(node.args.kwonlyargs) if arg.arg == "backend"
        )
        node.args.kwonlyargs[backend_index].annotation = ast.parse(
            "Workspace[WorkspaceT, BackendProtocol]"
            if workspace
            else "BackendProtocol | None",
            mode="eval",
        ).body
        node.args.kw_defaults[backend_index] = (
            None if workspace else ast.Constant(value=None)
        )
        workspace_type = "WorkspaceT" if workspace else "None"
        node.args.kwonlyargs.insert(
            backend_index + 1,
            ast.arg(
                arg="prepare_tools",
                annotation=ast.parse(
                    f"Callable[[AgentRunPreparation[{workspace_type}]], Awaitable[Sequence[BaseTool]]] | None",
                    mode="eval",
                ).body,
            ),
        )
        node.args.kw_defaults.insert(backend_index + 1, ast.Constant(value=None))
        subagents_index = next(
            i for i, arg in enumerate(node.args.kwonlyargs) if arg.arg == "subagents"
        )
        node.args.kwonlyargs[subagents_index].annotation = ast.parse(
            f"Sequence[SubAgent[{workspace_type}] | CompiledSubAgent | AsyncSubAgent] | None",
            mode="eval",
        ).body
        node.returns = ast.parse(
            "AgentRuntime[ContextT]" if typed else "AgentRuntime[None]", mode="eval"
        ).body
        declarations.append(
            _declaration(node, docstring=inspect.getdoc(TinkerFin().build))
        )
    return "\n".join(declarations)


def _render_init_stub() -> str:
    methods = [
        _method(TinkerFin.with_namespace, return_type="TinkerFin"),
        _method(TinkerFin.with_attachments, return_type="TinkerFin"),
        _method(
            TinkerFin.with_observer,
            replacements={
                "RuntimeObserver": "_RuntimeObserver",
                "TerminalObserver": "_TerminalObserver",
            },
            return_type="TinkerFin",
        ),
        _method(
            TinkerFin.with_plan,
            replacements={
                "ClarificationFormBase": "_ClarificationFormBase",
                "ClarificationType": "_ClarificationType",
                "DefaultClarificationForm": "_DefaultClarificationForm",
                "PlanContentModel": "_PlanContentModel",
                "PlanReviewAction": "_PlanReviewAction",
                "StructuredPlanContent": "_StructuredPlanContent",
                "DEFAULT_ALLOWED_REVIEW_ACTIONS": "_DEFAULT_ALLOWED_REVIEW_ACTIONS",
            },
            return_type="TinkerFin",
        ),
        _build_overloads(),
    ]
    content = "\n".join(
        [
            '"""Public builder and execution contracts generated from current source."""',
            "# Generated by scripts/generate_stubs.py; do not edit signatures manually.",
            _IMPORTS,
            "class TinkerFin(_RuntimeTinkerFin):",
            '    """Configure resources, choose a namespace, and build a Runtime."""',
            *methods,
            "__all__: list[str]",
        ]
    )
    return _format(content, target=_INIT_STUB)


def _render_build_stub() -> str:
    declarations = _build_overloads(method_name="__call__")
    required = {
        node.id
        for node in ast.walk(ast.parse("class BuildAgent:\n" + declarations))
        if isinstance(node, ast.Name)
    }
    imports: list[str] = []
    for statement in ast.parse(_IMPORTS).body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        statement.names = [
            alias
            for alias in statement.names
            if (alias.asname or alias.name) in required
        ]
        if statement.names:
            for alias in statement.names:
                if alias.asname == alias.name:
                    alias.asname = None
            imports.append(ast.unparse(statement))
    content = "\n".join(
        [
            '"""Typed callable contract for the actual Runtime build descriptor."""',
            "# Generated by scripts/generate_stubs.py; do not edit signatures manually.",
            "from __future__ import annotations",
            "from typing import Protocol",
            *imports,
            "class BuildAgent(Protocol):",
            '    """Build a Runtime whose execution context matches context_schema."""',
            declarations,
        ]
    )
    return _format(content, target=_BUILD_STUB)


def render_stubs() -> dict[Path, str]:
    return {_INIT_STUB: _render_init_stub(), _BUILD_STUB: _render_build_stub()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check generated stubs without writing files",
    )
    options = parser.parse_args()
    for path, content in render_stubs().items():
        if options.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                print(f"generated stub drift: {path}", file=sys.stderr)
                return 1
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
