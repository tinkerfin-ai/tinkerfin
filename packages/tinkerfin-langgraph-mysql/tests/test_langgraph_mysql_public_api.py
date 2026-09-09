"""Public distribution, error, and import-order contracts for the MySQL Store."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType

from langgraph.store.base import BaseStore

import tinkerfin_langgraph_mysql
from tinkerfin_langgraph_mysql import (
    AsyncMyStore,
    LangGraphMySQLDriverError,
    LangGraphMySQLError,
    LangGraphMySQLErrorCode,
    LangGraphMySQLSchemaError,
    LangGraphMySQLStoreClosedError,
)


def test_public_api_exposes_the_store_and_stable_error_boundary() -> None:
    assert tinkerfin_langgraph_mysql.__all__ == [
        "AsyncMyStore",
        "LangGraphMySQLDriverError",
        "LangGraphMySQLError",
        "LangGraphMySQLErrorCode",
        "LangGraphMySQLSchemaError",
        "LangGraphMySQLStoreClosedError",
    ]
    assert issubclass(AsyncMyStore, BaseStore)
    assert issubclass(LangGraphMySQLDriverError, LangGraphMySQLError)
    assert issubclass(LangGraphMySQLSchemaError, LangGraphMySQLError)
    assert issubclass(LangGraphMySQLStoreClosedError, LangGraphMySQLError)
    assert LangGraphMySQLDriverError.code is LangGraphMySQLErrorCode.DRIVER_FAILURE
    assert LangGraphMySQLSchemaError.code is LangGraphMySQLErrorCode.SCHEMA_MISMATCH
    assert LangGraphMySQLStoreClosedError.code is LangGraphMySQLErrorCode.STORE_CLOSED
    assert files("tinkerfin_langgraph_mysql").joinpath("py.typed").is_file()


def test_distribution_requires_asyncmy_without_other_mysql_drivers() -> None:
    requirements = tuple(distribution("tinkerfin-langgraph-mysql").requires or ())

    assert any(requirement.startswith("asyncmy") for requirement in requirements)
    assert all("aiomysql" not in requirement for requirement in requirements)
    assert all("pymysql" not in requirement for requirement in requirements)
    assert all(
        "langgraph-checkpoint-mysql" not in requirement for requirement in requirements
    )


def test_error_boundary_separates_public_and_diagnostic_context() -> None:
    cause = RuntimeError("driver detail")
    failure = LangGraphMySQLDriverError(operation="batch", cause=cause)

    assert failure.code is LangGraphMySQLErrorCode.DRIVER_FAILURE
    assert failure.context == {"operation": "batch"}
    assert failure.diagnostic_context == {"cause_type": "RuntimeError"}
    assert isinstance(failure.context, MappingProxyType)
    assert isinstance(failure.diagnostic_context, MappingProxyType)
    assert failure.cause is cause
    assert failure.__cause__ is cause


def test_import_does_not_load_unshipped_mysql_drivers() -> None:
    package_root = Path(__file__).parents[1] / "src"
    script = """
import builtins
import sys

original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == 'aiomysql' or name.startswith('aiomysql.'):
        raise AssertionError(f'unexpected aiomysql import: {name}')
    if name == 'pymysql' or name.startswith('pymysql.'):
        raise AssertionError(f'unexpected pymysql import: {name}')
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from tinkerfin_langgraph_mysql import AsyncMyStore
assert AsyncMyStore.__name__ == 'AsyncMyStore'
assert not any(name == 'aiomysql' or name.startswith('aiomysql.') for name in sys.modules)
assert not any(name == 'pymysql' or name.startswith('pymysql.') for name in sys.modules)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(package_root), environment.get("PYTHONPATH")))
    )

    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )


def test_canonical_and_facade_import_orders_are_independent() -> None:
    package_root = Path(__file__).parents[1] / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(package_root), environment.get("PYTHONPATH")))
    )
    scripts = (
        "from langgraph.store.mysql import AsyncMyStore; assert AsyncMyStore.__name__ == 'AsyncMyStore'",
        "from tinkerfin_langgraph_mysql import AsyncMyStore, LangGraphMySQLSchemaError; "
        "assert AsyncMyStore.__name__ == 'AsyncMyStore'; "
        "assert LangGraphMySQLSchemaError.__name__ == 'LangGraphMySQLSchemaError'",
    )

    for script in scripts:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
