"""Generated signatures and packaged exports match the current public API."""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import get_origin, get_type_hints

import pytest
from deepagents.graph import create_deep_agent

from tinkerfin import AgentRuntime, TinkerFin

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_INIT_STUB = _PACKAGE_ROOT / "src/tinkerfin/__init__.pyi"


def test_generated_stubs_have_no_drift() -> None:
    subprocess.run(
        [sys.executable, str(_PACKAGE_ROOT / "scripts/generate_stubs.py"), "--check"],
        cwd=_REPOSITORY_ROOT,
        check=True,
    )


def test_build_retains_factory_parameters_and_context_inference() -> None:
    module = ast.parse(_INIT_STUB.read_text(encoding="utf-8"))
    builder = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "TinkerFin"
    )
    overloads = [
        node
        for node in builder.body
        if isinstance(node, ast.FunctionDef) and node.name == "build"
    ]
    assert len(overloads) == 4
    expected = set(inspect.signature(create_deep_agent).parameters) | {"prepare_tools"}
    for overload in overloads:
        assert {arg.arg for arg in [*overload.args.args, *overload.args.kwonlyargs]} - {
            "self"
        } == expected
    assert overloads[0].returns is not None
    assert overloads[1].returns is not None
    assert ast.unparse(overloads[0].returns) == "AgentRuntime[ContextT]"
    assert ast.unparse(overloads[1].returns) == "AgentRuntime[None]"
    actual = TinkerFin().with_namespace("chosen").build
    assert set(inspect.signature(actual).parameters) == expected
    assert all(
        inspect.signature(actual)
        .parameters[name]
        .replace(annotation=parameter.annotation)
        == parameter
        for name, parameter in inspect.signature(create_deep_agent).parameters.items()
    )
    assert all(
        inspect.signature(actual).parameters[name].annotation == parameter.annotation
        for name, parameter in inspect.signature(create_deep_agent).parameters.items()
        if name not in {"backend", "subagents", "tools", "checkpointer", "cache"}
    )
    assert actual.__name__ == "build"
    assert get_origin(get_type_hints(actual)["return"]) is AgentRuntime
    assert get_origin(inspect.signature(actual).return_annotation) is AgentRuntime


def test_runtime_is_reexported_as_the_actual_execution_type() -> None:
    import tinkerfin.runtime as implementation

    assert AgentRuntime is implementation.AgentRuntime
    assert not issubclass(AgentRuntime, TinkerFin)
    assert not issubclass(TinkerFin, AgentRuntime)
    module = ast.parse(_INIT_STUB.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "runtime"
        and any(alias.name == alias.asname == "AgentRuntime" for alias in node.names)
        for node in module.body
    )


def test_removed_and_third_party_exports_are_not_advertised(tmp_path: Path) -> None:
    import tinkerfin
    import tinkerfin.deep_agent as graph_module

    for name in (
        "DeepAgentState",
        "create_deep_agent",
        "DeepAgentDefinition",
        "DeepAgentRuntime",
    ):
        assert not hasattr(tinkerfin, name)
    assert not hasattr(graph_module, "create_deep_agent")
    fixture = tmp_path / "removed_exports.py"
    fixture.write_text(
        "from tinkerfin import DeepAgentState, create_deep_agent, DeepAgentDefinition\n"
        "from tinkerfin.deep_agent import create_deep_agent as module_factory\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(Path(sys.executable).with_name("pyright")),
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
    assert completed.returncode == 1
    assert report["summary"]["errorCount"] == 4
    assert all(
        item["rule"] == "reportAttributeAccessIssue"
        for item in report["generalDiagnostics"]
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


def test_built_wheel_contains_the_public_types(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    source = tmp_path / "source"
    shutil.copytree(
        _PACKAGE_ROOT,
        source,
        ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"),
    )
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
            str(source),
        ],
        cwd=_REPOSITORY_ROOT,
        check=True,
    )
    with zipfile.ZipFile(next(output.glob("tinkerfin-*.whl"))) as archive:
        names = set(archive.namelist())
    assert {
        "tinkerfin/py.typed",
        "tinkerfin/__init__.pyi",
        "tinkerfin/runtime.py",
        "tinkerfin/deep_agent.py",
        "tinkerfin/_lazy_run.py",
        "tinkerfin/_build_api.py",
    } <= names
    assert "tinkerfin/deep_agent.pyi" not in names


@pytest.mark.parametrize("source", ["tinkerfin", "tinkerfin.runtime"])
@pytest.mark.parametrize("invalid", ["main", "child", "absent"])
def test_workspace_tool_types_reject_mismatched_capabilities(
    tmp_path: Path, source: str, invalid: str
) -> None:
    fixture = tmp_path / "workspace_contract.py"
    positive = (_PACKAGE_ROOT / "tests/typecheck_runtime_workspace.py").read_text()
    bad_arguments = {
        "main": "backend=workspace, prepare_tools=prepare_text",
        "child": "backend=workspace, subagents=[reader]",
        "absent": "prepare_tools=prepare_files",
    }[invalid]
    fixture.write_text(
        positive
        + f"\nfrom {source} import TinkerFin as Builder\n"
        + "\ndef invalid(workspace: Workspace[Path, BackendProtocol]) -> None:\n"
        + "    reader: SubAgent[str] = {\n"
        + '        "name": "reader", "description": "Read", "system_prompt": "Read",\n'
        + '        "prepare_tools": prepare_text,\n'
        + "    }\n"
        + f'    Builder().with_namespace("company").build(model="provider:model", {bad_arguments})\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(Path(sys.executable).with_name("pyright")),
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
    assert report["summary"]["filesAnalyzed"] == 1
    assert completed.returncode == 1 and report["summary"]["errorCount"] > 0
    failing_line = len(fixture.read_text().splitlines()) - 1
    assert all(
        diagnostic["range"]["start"]["line"] == failing_line
        and diagnostic["rule"] in {"reportCallIssue", "reportArgumentType"}
        for diagnostic in report["generalDiagnostics"]
    )


def test_workspace_build_types_are_complete_for_strict_callers(tmp_path: Path) -> None:
    fixture = tmp_path / "workspace_contract.py"
    shutil.copy2(_PACKAGE_ROOT / "tests/typecheck_runtime_workspace.py", fixture)
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": [fixture.name],
                "pythonVersion": "3.11",
                "typeCheckingMode": "strict",
                "reportMissingTypeStubs": "none",
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(Path(sys.executable).with_name("pyright")),
            "--pythonpath",
            sys.executable,
            "--project",
            str(config),
            "--outputjson",
        ],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["summary"]["filesAnalyzed"] == 1
    assert completed.returncode == 0 and report["summary"]["errorCount"] == 0, report
