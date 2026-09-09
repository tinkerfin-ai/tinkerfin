"""Generated type stubs remain aligned with the locked upstream APIs."""

from __future__ import annotations

import ast
import copy
import json
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


def _stub_methods(
    path: Path,
    class_name: str,
    method_name: str,
) -> list[ast.FunctionDef]:
    """Return every overload for one generated class method."""

    module = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]


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


def test_generated_stubs_preserve_upstream_options_with_explicit_agui_inputs() -> None:
    create_arguments = copy.deepcopy(
        _stub_method(_INIT_STUB, "TinkerFin", "create_deep_agent").args
    )
    create_arguments.args = create_arguments.args[1:]
    expected_create = copy.deepcopy(_upstream_function(create_deep_agent).args)
    assert ast.dump(create_arguments, include_attributes=False) == ast.dump(
        expected_create,
        include_attributes=False,
    )

    expected_astream = copy.deepcopy(
        _upstream_function(CompiledStateGraph.astream).args
    )
    _RenameAstreamTypes().visit(expected_astream)
    for index, argument in enumerate(expected_astream.kwonlyargs):
        if argument.arg == "output_keys":
            argument.annotation = ast.parse("None", mode="eval").body
            expected_astream.kw_defaults[index] = ast.Constant(value=None)
        elif argument.arg == "subgraphs":
            argument.annotation = ast.parse("Literal[True]", mode="eval").body
            expected_astream.kw_defaults[index] = ast.Constant(value=True)
        elif argument.arg == "version":
            argument.annotation = ast.parse('Literal["v2"]', mode="eval").body
            expected_astream.kw_defaults[index] = ast.Constant(value="v2")
    native = _stub_method(_DEEP_AGENT_STUB, "DeepAgentRuntime", "astream")
    assert ast.dump(native.args, include_attributes=False) == ast.dump(
        expected_astream,
        include_attributes=False,
    )

    ordinary = _stub_method(_DEEP_AGENT_STUB, "DeepAgentAgUiRuntime", "astream")
    expected_ordinary = copy.deepcopy(expected_astream)
    expected_ordinary.args[1].annotation = ast.Name(
        id="InputAgentState",
        ctx=ast.Load(),
    )
    assert ast.dump(ordinary.args, include_attributes=False) == ast.dump(
        expected_ordinary,
        include_attributes=False,
    )

    resumed = _stub_method(
        _DEEP_AGENT_STUB,
        "DeepAgentAgUiResumeRuntime",
        "astream",
    )
    assert [argument.arg for argument in resumed.args.args] == ["self"]
    assert [argument.arg for argument in resumed.args.kwonlyargs] == [
        "config",
        "context",
        "stream_mode",
        "print_mode",
        "output_keys",
        "interrupt_before",
        "interrupt_after",
        "durability",
        "control",
        "subgraphs",
        "debug",
        "version",
    ]


def test_generated_stub_declares_precise_facade_return_types() -> None:
    observe = _stub_method(_INIT_STUB, "TinkerFin", "observe")
    plan = _stub_method(_INIT_STUB, "TinkerFin", "plan")
    create = _stub_method(_INIT_STUB, "TinkerFin", "create_deep_agent")
    native_new = _stub_method(_DEEP_AGENT_STUB, "DeepAgentDefinition", "new")
    agui_new, resume_new = _stub_methods(
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
    resume_astream = _stub_method(
        _DEEP_AGENT_STUB,
        "DeepAgentAgUiResumeRuntime",
        "astream",
    )

    assert _return_type(observe) == "TinkerFin"
    assert _return_type(plan) == "TinkerFin"
    assert _return_type(create) == "DeepAgentDefinition[ContextT]"
    assert _return_type(native_new) == "DeepAgentRuntime[ContextT]"
    assert _return_type(agui_new) == "DeepAgentAgUiRuntime[ContextT]"
    assert _return_type(resume_new) == "DeepAgentAgUiResumeRuntime[ContextT]"
    assert _return_type(native_astream) == "NativeGraphRunStream"
    assert _return_type(agui_astream) == "AgUiEventStream"
    assert _return_type(resume_astream) == "AgUiEventStream"
    assert "ParamSpec" not in _INIT_STUB.read_text(encoding="utf-8")
    assert "ParamSpec" not in _DEEP_AGENT_STUB.read_text(encoding="utf-8")


def test_root_stub_does_not_export_low_level_source_contracts() -> None:
    module = ast.parse(_INIT_STUB.read_text(encoding="utf-8"))
    imported = {
        alias.asname or alias.name
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert {
        "AgUiNativeStreamConfig",
        "AgUiNativeStreamInvocation",
        "GraphRunStream",
        "NativeTinkerFinRun",
        "TinkerFinRun",
    }.isdisjoint(imported)


def test_generated_stubs_reject_third_party_ghost_exports(tmp_path: Path) -> None:
    import tinkerfin
    import tinkerfin.deep_agent as deep_agent_module

    assert not hasattr(tinkerfin, "DeepAgentState")
    assert not hasattr(tinkerfin, "create_deep_agent")
    assert not hasattr(deep_agent_module, "create_deep_agent")
    for stub in (_INIT_STUB, _DEEP_AGENT_STUB):
        module = ast.parse(stub.read_text(encoding="utf-8"))
        assert all(
            alias.name != "*"
            for node in module.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        )

    fixture = tmp_path / "ghost_exports.py"
    fixture.write_text(
        "from tinkerfin import DeepAgentState, create_deep_agent\n"
        "from tinkerfin.deep_agent import create_deep_agent as module_factory\n",
        encoding="utf-8",
    )
    pyright = Path(sys.executable).with_name("pyright")
    completed = subprocess.run(
        [
            str(pyright),
            "--pythonpath",
            sys.executable,
            "--outputjson",
            str(fixture),
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    diagnostics = report["generalDiagnostics"]

    assert completed.returncode == 1
    assert report["summary"]["errorCount"] == 3
    assert all(
        diagnostic["rule"] == "reportAttributeAccessIssue"
        and "unknown import symbol" in diagnostic["message"]
        for diagnostic in diagnostics
    )


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
        "ClarificationType": "_ClarificationType",
        "DefaultClarificationForm": "_DefaultClarificationForm",
        "PlanContentModel": "_PlanContentModel",
        "PlanReviewAction": "_PlanReviewAction",
        "StructuredPlanContent": "_StructuredPlanContent",
    }
    plan = _stub_method(_INIT_STUB, "TinkerFin", "plan")
    index = next(
        index
        for index, argument in enumerate(plan.args.kwonlyargs)
        if argument.arg == "clarification_schema"
    )
    annotation = plan.args.kwonlyargs[index].annotation
    assert annotation is not None
    assert ast.unparse(annotation) == "type[_ClarificationFormBase]"
    default = plan.args.kw_defaults[index]
    assert isinstance(default, ast.Name)
    assert default.id == "_DefaultClarificationForm"
    content_schema_index = next(
        index
        for index, argument in enumerate(plan.args.kwonlyargs)
        if argument.arg == "content_schema"
    )
    content_schema_annotation = plan.args.kwonlyargs[content_schema_index].annotation
    assert content_schema_annotation is not None
    assert ast.unparse(content_schema_annotation) == "type[_PlanContentModel]"
    content_schema_default = plan.args.kw_defaults[content_schema_index]
    assert isinstance(content_schema_default, ast.Name)
    assert content_schema_default.id == "_StructuredPlanContent"
    allowed_review_actions_index = next(
        index
        for index, argument in enumerate(plan.args.kwonlyargs)
        if argument.arg == "allowed_review_actions"
    )
    allowed_review_actions_annotation = plan.args.kwonlyargs[
        allowed_review_actions_index
    ].annotation
    assert allowed_review_actions_annotation is not None
    assert ast.unparse(allowed_review_actions_annotation) == (
        "Sequence[_PlanReviewAction]"
    )
    allowed_review_actions_default = plan.args.kw_defaults[allowed_review_actions_index]
    assert isinstance(allowed_review_actions_default, ast.Name)
    assert allowed_review_actions_default.id == "_DEFAULT_ALLOWED_REVIEW_ACTIONS"


def test_built_wheel_contains_the_generated_stubs(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    # Wheel contents are the contract under test. Offline resolution keeps this check
    # from becoming an unrelated package-index availability probe.
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
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
    assert "tinkerfin/agui_native.py" not in names
    assert not any("-v1" in name for name in names)


def test_definition_stub_methods_follow_the_runtime_implementation() -> None:
    stub = _stub_method(_DEEP_AGENT_STUB, "DeepAgentDefinition", "new")
    expected = _upstream_function(DeepAgentDefinition.new)
    assert ast.dump(stub.args, include_attributes=False) == ast.dump(
        expected.args,
        include_attributes=False,
    )

    runtime = _upstream_function(DeepAgentDefinition.new_agui)
    runtime_names = [argument.arg for argument in runtime.args.kwonlyargs]
    overloads = _stub_methods(
        _DEEP_AGENT_STUB,
        "DeepAgentDefinition",
        "new_agui",
    )
    assert len(overloads) == 2
    assert all(
        [argument.arg for argument in overload.args.kwonlyargs] == runtime_names
        for overload in overloads
    )
    assert "run_input" not in runtime_names
    assert "parent_run_id" in runtime_names
    assert "on_resume_checkpointed" in runtime_names


def test_resume_preparation_remains_async_in_generated_stub() -> None:
    module = ast.parse(_DEEP_AGENT_STUB.read_text(encoding="utf-8"))
    definition = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "DeepAgentDefinition"
    )
    resume = next(
        node
        for node in definition.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "prepare_agui_resume"
    )

    assert isinstance(resume, ast.AsyncFunctionDef)
