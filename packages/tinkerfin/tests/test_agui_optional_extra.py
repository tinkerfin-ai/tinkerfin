"""Core and AG-UI extra installation boundaries."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def test_core_and_plan_import_without_agui_and_entrypoint_error_is_precise() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = r"""
import importlib.abc
import sys


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (
            fullname == "ag_ui"
            or fullname.startswith("ag_ui.")
            or fullname == "tinkerfin_agui_adapter"
            or fullname.startswith("tinkerfin_agui_adapter.")
        ):
            error = ModuleNotFoundError(f"blocked {fullname}")
            error.name = fullname.split(".")[0]
            raise error
        return None


sys.meta_path.insert(0, Blocker())
import tinkerfin

configured = tinkerfin.TinkerFin().plan(default_mode="plan")
definition = configured.create_deep_agent(model="provider:model", tools=[])
assert definition
assert not any(
    name == "ag_ui"
    or name.startswith("ag_ui.")
    or name == "tinkerfin_agui_adapter"
    or name.startswith("tinkerfin_agui_adapter.")
    for name in sys.modules
)

for operation in (
    lambda: tinkerfin.AgUiResumeBinding,
    lambda: definition.new_agui(
        identity=tinkerfin.RunIdentity(threadId="thread-core", runId="run-core")
    ),
):
    try:
        operation()
    except ModuleNotFoundError as error:
        assert 'pip install "tinkerfin[agui]"' in str(error)
    else:
        raise AssertionError("AG-UI entrypoint unexpectedly loaded without its extra")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_distribution_metadata_declares_current_optional_dependency_graph() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    tinkerfin_project = tomllib.loads(
        (repository_root / "packages/tinkerfin/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    messaging_project = tomllib.loads(
        (repository_root / "packages/tinkerfin-messaging/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    studio_project = tomllib.loads(
        (repository_root / "apps/studio/server/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]

    core_dependencies = tuple(tinkerfin_project["dependencies"])
    assert not any(value.startswith("ag-ui-protocol") for value in core_dependencies)
    assert not any(
        value.startswith("tinkerfin-agui-adapter") for value in core_dependencies
    )
    assert tinkerfin_project["optional-dependencies"]["agui"] == [
        "ag-ui-protocol==0.1.19",
        "tinkerfin-agui-adapter>=0.1.0,<0.9.0",
    ]
    assert messaging_project["optional-dependencies"]["native"] == [
        "tinkerfin-native-stream>=0.1.0,<0.9.0"
    ]
    assert "tinkerfin[agui,redis]>=0.1.0,<0.9.0" in studio_project["dependencies"]
