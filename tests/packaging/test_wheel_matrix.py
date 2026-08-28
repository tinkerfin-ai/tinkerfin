"""Build and install every public wheel boundary in isolated environments."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_SCRIPT = _ROOT / "apps" / "studio" / "server" / "deploy" / "deploy.sh"


@dataclass(frozen=True, slots=True)
class _WheelProject:
    """Describe one local distribution and its required wheel artifacts."""

    name: str
    path: Path
    marker: str | None


@dataclass(frozen=True, slots=True)
class _InstallCase:
    """Describe one independently resolvable public installation boundary."""

    name: str
    spec: str
    imports: tuple[str, ...]
    required_distributions: tuple[str, ...]
    forbidden_distributions: tuple[str, ...] = ()
    forbidden_modules: tuple[str, ...] = ()
    smoke: str | None = None
    full_matrix_only: bool = False


_PROJECTS = (
    _WheelProject(
        "tinkerfin-contracts",
        _ROOT / "packages/tinkerfin-contracts",
        "tinkerfin_contracts/py.typed",
    ),
    _WheelProject(
        "tinkerfin-native-stream",
        _ROOT / "packages/tinkerfin-native-stream",
        "tinkerfin_native_stream/py.typed",
    ),
    _WheelProject(
        "tinkerfin-agui-adapter",
        _ROOT / "packages/tinkerfin-agui-adapter",
        "tinkerfin_agui_adapter/py.typed",
    ),
    _WheelProject("tinkerfin", _ROOT / "packages/tinkerfin", "tinkerfin/py.typed"),
    _WheelProject(
        "tinkerfin-messaging",
        _ROOT / "packages/tinkerfin-messaging",
        "tinkerfin_messaging/py.typed",
    ),
    _WheelProject(
        "tinkerfin-tracing",
        _ROOT / "packages/tinkerfin-tracing",
        "tinkerfin_tracing/py.typed",
    ),
    _WheelProject(
        "tinkerfin-sandbox",
        _ROOT / "packages/tinkerfin-sandbox",
        "tinkerfin_sandbox/py.typed",
    ),
    _WheelProject(
        "tinkerfin-langgraph-mysql",
        _ROOT / "packages/tinkerfin-langgraph-mysql",
        "tinkerfin_langgraph_mysql/py.typed",
    ),
    _WheelProject("tinkerfin-studio", _ROOT / "apps/studio/server", None),
)

_CORE_CASES = (
    _InstallCase(
        "contracts-core",
        "tinkerfin-contracts==0.1.0",
        ("tinkerfin_contracts",),
        ("tinkerfin-contracts",),
        ("tinkerfin", "ag-ui-protocol", "redis", "sqlalchemy"),
        ("tinkerfin", "ag_ui", "redis", "sqlalchemy"),
    ),
    _InstallCase(
        "native-stream-core",
        "tinkerfin-native-stream==0.1.0",
        ("tinkerfin_native_stream",),
        (
            "tinkerfin-native-stream",
            "tinkerfin-contracts",
            "langchain-core",
            "langgraph",
        ),
        ("tinkerfin", "tinkerfin-agui-adapter", "deepagents", "redis", "sqlalchemy"),
        ("tinkerfin", "tinkerfin_agui_adapter", "deepagents", "redis", "sqlalchemy"),
    ),
    _InstallCase(
        "adapter-core",
        "tinkerfin-agui-adapter==0.1.0",
        ("tinkerfin_agui_adapter",),
        ("tinkerfin-agui-adapter", "ag-ui-protocol", "tinkerfin-native-stream"),
        ("tinkerfin", "deepagents", "redis", "sqlalchemy"),
        ("tinkerfin", "deepagents", "redis", "sqlalchemy"),
    ),
    _InstallCase(
        "runtime-core",
        "tinkerfin==0.1.0",
        ("tinkerfin",),
        ("tinkerfin", "deepagents", "langchain", "langchain-core", "langgraph"),
        ("ag-ui-protocol", "tinkerfin-agui-adapter", "redis"),
        ("ag_ui", "tinkerfin_agui_adapter", "redis"),
    ),
    _InstallCase(
        "messaging-core",
        "tinkerfin-messaging==0.1.0",
        ("tinkerfin_messaging",),
        ("tinkerfin-messaging", "tinkerfin-contracts"),
        ("tinkerfin", "tinkerfin-native-stream", "ag-ui-protocol", "redis"),
        ("tinkerfin", "tinkerfin_native_stream", "ag_ui", "redis"),
    ),
    _InstallCase(
        "tracing-core",
        "tinkerfin-tracing==0.1.0",
        ("tinkerfin_tracing",),
        ("tinkerfin-tracing", "tinkerfin-contracts"),
        ("tinkerfin", "sqlalchemy", "aiosqlite", "asyncmy"),
        ("tinkerfin", "sqlalchemy", "aiosqlite", "asyncmy"),
    ),
    _InstallCase(
        "sandbox-core",
        "tinkerfin-sandbox==0.1.0",
        ("tinkerfin_sandbox",),
        ("tinkerfin-sandbox", "deepagents", "opensandbox"),
        ("sqlalchemy", "aiosqlite", "asyncmy"),
        ("sqlalchemy", "aiosqlite", "asyncmy"),
    ),
    _InstallCase(
        "langgraph-mysql-core",
        "tinkerfin-langgraph-mysql==0.1.0",
        ("langgraph.store.mysql", "tinkerfin_langgraph_mysql"),
        ("tinkerfin-langgraph-mysql", "asyncmy", "langgraph-checkpoint", "orjson"),
        ("aiomysql", "pymysql", "langgraph-checkpoint-mysql"),
        ("aiomysql", "pymysql"),
    ),
)

_FULL_CASES = (
    _InstallCase(
        "runtime-agui",
        "tinkerfin[agui]==0.1.0",
        ("tinkerfin", "tinkerfin_agui_adapter", "ag_ui"),
        ("tinkerfin", "tinkerfin-agui-adapter", "ag-ui-protocol"),
        ("redis",),
        ("redis",),
        full_matrix_only=True,
    ),
    _InstallCase(
        "runtime-redis",
        "tinkerfin[redis]==0.1.0",
        ("tinkerfin", "redis"),
        ("tinkerfin", "redis"),
        ("ag-ui-protocol", "tinkerfin-agui-adapter"),
        ("ag_ui", "tinkerfin_agui_adapter"),
        full_matrix_only=True,
    ),
    _InstallCase(
        "runtime-agui-redis",
        "tinkerfin[agui,redis]==0.1.0",
        ("tinkerfin", "tinkerfin_agui_adapter", "ag_ui", "redis"),
        ("tinkerfin", "tinkerfin-agui-adapter", "ag-ui-protocol", "redis"),
        full_matrix_only=True,
    ),
    _InstallCase(
        "messaging-agui",
        "tinkerfin-messaging[agui]==0.1.0",
        ("tinkerfin_messaging", "ag_ui"),
        ("tinkerfin-messaging", "ag-ui-protocol"),
        ("tinkerfin", "redis", "tinkerfin-native-stream"),
        ("tinkerfin", "redis", "tinkerfin_native_stream"),
        full_matrix_only=True,
    ),
    _InstallCase(
        "messaging-native",
        "tinkerfin-messaging[native]==0.1.0",
        ("tinkerfin_messaging", "tinkerfin_native_stream"),
        ("tinkerfin-messaging", "tinkerfin-native-stream"),
        ("tinkerfin", "deepagents", "ag-ui-protocol", "redis"),
        ("tinkerfin", "deepagents", "ag_ui", "redis"),
        full_matrix_only=True,
    ),
    _InstallCase(
        "messaging-redis",
        "tinkerfin-messaging[redis]==0.1.0",
        ("tinkerfin_messaging", "redis"),
        ("tinkerfin-messaging", "redis"),
        ("tinkerfin", "ag-ui-protocol", "tinkerfin-native-stream"),
        ("tinkerfin", "ag_ui", "tinkerfin_native_stream"),
        full_matrix_only=True,
    ),
    _InstallCase(
        "messaging-agui-redis",
        "tinkerfin-messaging[agui,redis]==0.1.0",
        ("tinkerfin_messaging", "ag_ui", "redis"),
        ("tinkerfin-messaging", "ag-ui-protocol", "redis"),
        ("tinkerfin", "tinkerfin-native-stream"),
        ("tinkerfin", "tinkerfin_native_stream"),
        full_matrix_only=True,
    ),
    _InstallCase(
        "tracing-sqlite",
        "tinkerfin-tracing[sqlite]==0.1.0",
        ("tinkerfin_tracing", "sqlalchemy", "aiosqlite"),
        ("tinkerfin-tracing", "sqlalchemy", "aiosqlite"),
        ("asyncmy",),
        ("asyncmy",),
        smoke="trace_sqlite",
        full_matrix_only=True,
    ),
    _InstallCase(
        "tracing-mysql",
        "tinkerfin-tracing[mysql]==0.1.0",
        ("tinkerfin_tracing", "sqlalchemy", "asyncmy"),
        ("tinkerfin-tracing", "sqlalchemy", "asyncmy"),
        ("aiosqlite",),
        ("aiosqlite",),
        full_matrix_only=True,
    ),
    _InstallCase(
        "sandbox-sqlite",
        "tinkerfin-sandbox[sqlite]==0.1.0",
        ("tinkerfin_sandbox", "sqlalchemy", "aiosqlite"),
        ("tinkerfin-sandbox", "sqlalchemy", "aiosqlite"),
        ("asyncmy",),
        ("asyncmy",),
        smoke="sandbox_sqlite",
        full_matrix_only=True,
    ),
    _InstallCase(
        "sandbox-mysql",
        "tinkerfin-sandbox[mysql]==0.1.0",
        ("tinkerfin_sandbox", "sqlalchemy", "asyncmy"),
        ("tinkerfin-sandbox", "sqlalchemy", "asyncmy"),
        ("aiosqlite",),
        ("aiosqlite",),
        full_matrix_only=True,
    ),
)

_STUDIO_CASE = _InstallCase(
    "studio-production",
    "tinkerfin-studio==0.1.0",
    (
        "tinkerfin_studio",
        "tinkerfin",
        "tinkerfin_langgraph_mysql",
        "tinkerfin_messaging",
        "tinkerfin_sandbox",
        "tinkerfin_tracing",
    ),
    (
        "tinkerfin-studio",
        "tinkerfin",
        "tinkerfin-langgraph-mysql",
        "tinkerfin-messaging",
        "tinkerfin-sandbox",
        "tinkerfin-tracing",
    ),
    ("aiomysql", "pymysql", "langgraph-checkpoint-mysql"),
    ("aiomysql", "pymysql"),
)

_CASES = (*_CORE_CASES, *_FULL_CASES, _STUDIO_CASE)


def _release_project_paths() -> tuple[str, ...]:
    script = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"readonly -a PROJECT_PATHS=\(\n(?P<body>.*?)\n\)",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    return tuple(shlex.split(match.group("body")))


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _source_python_payload(source_root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".pyi"}
    }


def _wheel_python_payload(archive: zipfile.ZipFile) -> dict[str, bytes]:
    return {
        name: archive.read(name)
        for name in archive.namelist()
        if Path(name).suffix in {".py", ".pyi"}
    }


@pytest.mark.packaging_e2e
def test_python_payload_comparison_detects_same_path_stale_content(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    source_module = source_root / "example" / "module.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("CURRENT = True\n", encoding="utf-8")
    wheel = tmp_path / "stale.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("example/module.py", "CURRENT = False\n")
    with zipfile.ZipFile(wheel) as archive:
        wheel_payload = _wheel_python_payload(archive)

    assert wheel_payload != _source_python_payload(source_root)


def _is_local_import(source_root: Path, module: str) -> bool:
    module_path = source_root.joinpath(*module.split("."))
    return module_path.is_dir() or module_path.with_suffix(".py").is_file()


@pytest.mark.packaging_e2e
def test_first_party_projects_declare_every_direct_import_distribution() -> None:
    module_distributions = importlib.metadata.packages_distributions()
    violations: list[str] = []
    for project in _PROJECTS:
        source_root = project.path / "src"
        pyproject = tomllib.loads(
            (project.path / "pyproject.toml").read_text(encoding="utf-8")
        )
        project_metadata = pyproject["project"]
        declared = {
            canonicalize_name(Requirement(requirement).name)
            for requirement in project_metadata.get("dependencies", [])
        }
        for requirements in project_metadata.get("optional-dependencies", {}).values():
            declared.update(
                canonicalize_name(Requirement(requirement).name)
                for requirement in requirements
            )

        imports: set[str] = set()
        for source in source_root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module is not None
                ):
                    imports.add(node.module)

        for module in sorted(imports):
            root_module = module.partition(".")[0]
            if root_module in sys.stdlib_module_names or _is_local_import(
                source_root, module
            ):
                continue
            distributions = {
                canonicalize_name(distribution)
                for distribution in module_distributions.get(root_module, ())
            }
            if not distributions:
                violations.append(
                    f"{project.name}: import {module!r} has no installed distribution"
                )
            elif declared.isdisjoint(distributions):
                violations.append(
                    f"{project.name}: import {module!r} requires one of "
                    f"{sorted(distributions)!r}"
                )

    assert violations == []


@pytest.fixture(scope="session")
def wheel_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build current wheels once and verify package metadata and stale-file absence."""

    output = tmp_path_factory.mktemp("wheel-matrix-dist")
    for project in _PROJECTS:
        _run(
            [
                "uv",
                "build",
                "--offline",
                "--quiet",
                "--wheel",
                "--out-dir",
                str(output),
                "--no-create-gitignore",
                str(project.path),
            ]
        )
    wheels = tuple(output.glob("*.whl"))
    assert len(wheels) == len(_PROJECTS)
    for project in _PROJECTS:
        prefix = project.name.replace("-", "_") + "-"
        wheel = next(path for path in wheels if path.name.startswith(prefix))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata_name = next(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            metadata = archive.read(metadata_name).decode("utf-8")
            wheel_python_payload = _wheel_python_payload(archive)
        source_root = project.path / "src"
        assert wheel_python_payload == _source_python_payload(source_root)
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
        assert "Description-Content-Type: text/markdown" in metadata
        if project.marker is not None:
            assert project.marker in names
        if project.name == "tinkerfin-langgraph-mysql":
            assert any(name.endswith(".dist-info/licenses/NOTICE") for name in names)
            assert "langgraph/store/mysql/py.typed" in names
            assert not any(
                name.startswith("langgraph/checkpoint/mysql/") for name in names
            )
            assert not any("store_migrations" in name for name in names)
        if project.name == "tinkerfin":
            assert "tinkerfin/agui_native.py" not in names
        if project.name == "tinkerfin-agui-adapter":
            assert not any("-v1" in name for name in names)
    return output


def _verification_script() -> str:
    return r"""
import asyncio
import importlib
import importlib.metadata
import json
import os
import sys

case = json.loads(os.environ["TINKERFIN_WHEEL_CASE"])
for module in case["imports"]:
    importlib.import_module(module)
for name in case["required_distributions"]:
    importlib.metadata.distribution(name)
for name in case["forbidden_distributions"]:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError(f"forbidden distribution installed: {name}")
for root in case["forbidden_modules"]:
    if any(name == root or name.startswith(root + ".") for name in sys.modules):
        raise AssertionError(f"forbidden module loaded: {root}")

async def smoke() -> None:
    if case["smoke"] == "trace_sqlite":
        from sqlalchemy.ext.asyncio import create_async_engine
        from tinkerfin_tracing import SqlAlchemyTraceStore
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await SqlAlchemyTraceStore(engine, namespace="wheel-matrix").setup()
        finally:
            await engine.dispose()
    elif case["smoke"] == "sandbox_sqlite":
        from tinkerfin_sandbox import SQLAlchemyOpenSandboxState
        state = SQLAlchemyOpenSandboxState(
            url="sqlite+aiosqlite:///:memory:",
            namespace="wheel-matrix",
        )
        try:
            await state.start(warm_pool_size=0)
        finally:
            await state.aclose()

asyncio.run(smoke())
"""


@pytest.mark.packaging_e2e
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_isolated_wheel_installation(
    case: _InstallCase,
    wheel_directory: Path,
    tmp_path: Path,
) -> None:
    """Resolve, import, and smoke-test one public installation combination."""

    if case.full_matrix_only and sys.version_info[:2] != (3, 11):
        pytest.skip("single-extra combinations run on the Python 3.11 full matrix")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["TINKERFIN_WHEEL_CASE"] = json.dumps(
        {
            "imports": case.imports,
            "required_distributions": case.required_distributions,
            "forbidden_distributions": case.forbidden_distributions,
            "forbidden_modules": case.forbidden_modules,
            "smoke": case.smoke,
        }
    )
    venv = tmp_path / "venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--find-links",
            str(wheel_directory),
            case.spec,
        ],
        environment=environment,
    )
    _run(["uv", "pip", "check", "--python", str(python)], environment=environment)
    _run(
        [str(python), "-I", "-c", _verification_script()],
        environment=environment,
    )


@pytest.mark.packaging_e2e
def test_studio_deploy_wheel_set_is_self_contained(
    wheel_directory: Path,
    tmp_path: Path,
) -> None:
    """Install the exact deployment wheel set after external locked dependencies."""

    if sys.version_info[:2] != (3, 11):
        pytest.skip("the production deployment set runs on the Python 3.11 matrix")

    expected_paths = tuple(
        str(project.path.relative_to(_ROOT)) for project in _PROJECTS
    )
    release_paths = _release_project_paths()
    assert release_paths == expected_paths

    requirements = tmp_path / "requirements.txt"
    _run(
        [
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--package",
            "tinkerfin-studio",
            "--no-dev",
            "--no-emit-workspace",
            "--no-header",
            "--format",
            "requirements.txt",
            "--output-file",
            str(requirements),
        ]
    )

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    venv = tmp_path / "deploy-venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(requirements),
        ],
        environment=environment,
    )

    projects_by_path = {
        str(project.path.relative_to(_ROOT)): project for project in _PROJECTS
    }
    release_wheels: list[str] = []
    for project_path in release_paths:
        project = projects_by_path[project_path]
        prefix = project.name.replace("-", "_") + "-"
        wheel = next(
            path
            for path in wheel_directory.glob("*.whl")
            if path.name.startswith(prefix)
        )
        release_wheels.append(str(wheel))
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            *release_wheels,
        ],
        environment=environment,
    )
    _run(["uv", "pip", "check", "--python", str(python)], environment=environment)
    _run(
        [str(python), "-I", "-c", "import tinkerfin_studio.application"],
        environment=environment,
    )
