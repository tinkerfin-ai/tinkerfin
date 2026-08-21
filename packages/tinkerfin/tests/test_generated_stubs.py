"""Generated type stubs remain aligned with the locked upstream APIs."""

from __future__ import annotations

import ast
import copy
import subprocess
import sys
import textwrap
import zipfile
from collections.abc import Callable
from pathlib import Path

from deepagents.graph import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from tinkerfin import DeepAgentDefinition

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_GENERATOR = _PACKAGE_ROOT / "scripts/generate_stubs.py"
_PACKAGE = _PACKAGE_ROOT / "src/tinkerfin"
_DEEP_AGENT_STUB = _PACKAGE / "deep_agent.pyi"
_INIT_STUB = _PACKAGE / "__init__.pyi"


def _stub_method(
    path: Path,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


def _upstream_function(
    function: Callable[..., object],
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    module = ast.parse(textwrap.dedent(inspect_source(function)))
    return next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _return_type(method: ast.FunctionDef) -> str:
    returns = method.returns
    assert returns is not None
    return ast.unparse(returns)


def inspect_source(function: Callable[..., object]) -> str:
    import inspect

    return inspect.getsource(function)


class _RenameAstreamTypes(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.Name:
        return ast.copy_location(
            ast.Name(
                id="InputAgentState" if node.id == "InputT" else node.id,
                ctx=node.ctx,
            ),
            node,
        )


def test_generated_stubs_exist_and_the_generator_reports_no_drift() -> None:
    assert _GENERATOR.is_file()
    assert _DEEP_AGENT_STUB.is_file()
    assert _INIT_STUB.is_file()

    completed = subprocess.run(
        [sys.executable, str(_GENERATOR), "--check"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_generated_stubs_match_both_upstream_parameter_lists() -> None:
    create_arguments = copy.deepcopy(
        _stub_method(_INIT_STUB, "TinkerFin", "create_deep_agent").args
    )
    create_arguments.args = create_arguments.args[1:]
    assert ast.dump(create_arguments, include_attributes=False) == ast.dump(
        _upstream_function(create_deep_agent).args,
        include_attributes=False,
    )

    expected_astream = copy.deepcopy(
        _upstream_function(CompiledStateGraph.astream).args
    )
    _RenameAstreamTypes().visit(expected_astream)
    for runtime_name in ("DeepAgentRuntime", "DeepAgentAgUiRuntime"):
        actual = _stub_method(_DEEP_AGENT_STUB, runtime_name, "astream")
        assert ast.dump(actual.args, include_attributes=False) == ast.dump(
            expected_astream,
            include_attributes=False,
        )


def test_generated_stub_declares_precise_facade_return_types() -> None:
    plan = _stub_method(_INIT_STUB, "TinkerFin", "plan")
    create = _stub_method(_INIT_STUB, "TinkerFin", "create_deep_agent")
    native_new = _stub_method(_DEEP_AGENT_STUB, "DeepAgentDefinition", "new")
    agui_new = _stub_method(
        _DEEP_AGENT_STUB,
        "DeepAgentDefinition",
        "new_agui",
    )
    native_astream = _stub_method(
        _DEEP_AGENT_STUB,
        "DeepAgentRuntime",
        "astream",
    )
    agui_astream = _stub_method(
        _DEEP_AGENT_STUB,
        "DeepAgentAgUiRuntime",
        "astream",
    )

    assert _return_type(plan) == "TinkerFin"
    assert _return_type(create) == "DeepAgentDefinition[ContextT]"
    assert _return_type(native_new) == "DeepAgentRuntime[ContextT]"
    assert _return_type(agui_new) == "DeepAgentAgUiRuntime[ContextT]"
    assert _return_type(native_astream) == "NativeGraphRunStream"
    assert _return_type(agui_astream) == "AgUiEventStream"
    assert "ParamSpec" not in _INIT_STUB.read_text(encoding="utf-8")
    assert "ParamSpec" not in _DEEP_AGENT_STUB.read_text(encoding="utf-8")


def test_root_stub_keeps_plan_annotation_dependencies_private() -> None:
    module = ast.parse(_INIT_STUB.read_text(encoding="utf-8"))
    aliases = {
        alias.name: alias.asname
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module == "plan"
        for alias in node.names
        if alias.name != "AgentMode"
    }

    assert aliases == {
        "ClarificationFormBase": "_ClarificationFormBase",
        "DefaultClarificationForm": "_DefaultClarificationForm",
    }
    plan = _stub_method(_INIT_STUB, "TinkerFin", "plan")
    index = next(
        index
        for index, argument in enumerate(plan.args.kwonlyargs)
        if argument.arg == "clarification_schema"
    )
    assert ast.unparse(plan.args.kwonlyargs[index].annotation) == (
        "type[_ClarificationFormBase]"
    )
    default = plan.args.kw_defaults[index]
    assert isinstance(default, ast.Name)
    assert default.id == "_DefaultClarificationForm"


def test_built_wheel_contains_the_generated_stubs(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "--quiet",
            "--wheel",
            "--out-dir",
            str(output),
            "--no-create-gitignore",
            str(_PACKAGE_ROOT),
        ],
        cwd=_REPOSITORY_ROOT,
        check=True,
    )
    wheel = next(output.glob("tinkerfin-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "tinkerfin/py.typed" in names
    assert "tinkerfin/agui_resume.py" in names
    assert "tinkerfin/plan/__init__.py" in names
    assert "tinkerfin/plan/clarification.py" in names
    assert "tinkerfin/__init__.pyi" in names
    assert "tinkerfin/deep_agent.pyi" in names


def test_definition_stub_methods_follow_the_runtime_implementation() -> None:
    for method_name in ("new", "new_agui"):
        stub = _stub_method(_DEEP_AGENT_STUB, "DeepAgentDefinition", method_name)
        runtime_method = getattr(DeepAgentDefinition, method_name)
        expected = _upstream_function(runtime_method)
        assert ast.dump(stub.args, include_attributes=False) == ast.dump(
            expected.args,
            include_attributes=False,
        )
