"""Distribution and dependency boundaries for the leaf contracts package."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

import pytest


def test_contracts_import_loads_no_framework_or_infrastructure_packages() -> None:
    repository = Path(__file__).resolve().parents[3]
    script = """
import json
import sys
import tinkerfin_contracts

blocked = (
    'ag_ui', 'deepagents', 'langchain', 'langgraph', 'redis', 'sqlalchemy',
    'tinkerfin', 'tinkerfin_agui_adapter', 'tinkerfin_messaging',
    'tinkerfin_studio',
)
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + '.') for prefix in blocked)
)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_distribution_declares_only_pydantic() -> None:
    try:
        metadata = distribution("tinkerfin-contracts")
    except PackageNotFoundError:
        pytest.fail("tinkerfin-contracts distribution is not installed")

    assert set(metadata.requires or ()) == {"pydantic<3,>=2"}
    assert files("tinkerfin_contracts").joinpath("py.typed").is_file()
