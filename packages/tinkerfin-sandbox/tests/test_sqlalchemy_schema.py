from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, is_dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import aiosqlite
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.sql import Select

import tinkerfin_sandbox
from tinkerfin_sandbox.lifecycle import sqlalchemy as sqlalchemy_lifecycle

_TABLE_NAMES = (
    "tinkerfin_opensandbox_cleanup",
    "tinkerfin_opensandbox_owners",
    "tinkerfin_opensandbox_warm_slots",
    "tinkerfin_opensandbox_workers",
)

_INDEX_NAMES = (
    "ix_tinkerfin_opensandbox_cleanup_lease",
    "ix_tinkerfin_opensandbox_owners_lease",
    "ix_tinkerfin_opensandbox_warm_slots_available",
    "ix_tinkerfin_opensandbox_workers_lease",
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _sqlite_schema_objects(path: Path, *, kind: str) -> tuple[str, ...]:
    async with aiosqlite.connect(path) as connection:
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name",
            (kind,),
        )
        rows = await cursor.fetchall()
    return tuple(str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_"))


async def test_sqlite_runtime_schema_characterization(tmp_path: Path) -> None:
    """Runtime initialization creates the complete current schema."""
    database_path = tmp_path / "runtime-schema.db"
    state_type = tinkerfin_sandbox.SQLAlchemyOpenSandboxState
    state = state_type(url=_sqlite_url(database_path), namespace="test")
    await state.start(warm_pool_size=0)
    await state.aclose()

    assert await _sqlite_schema_objects(database_path, kind="table") == _TABLE_NAMES
    assert await _sqlite_schema_objects(database_path, kind="index") == _INDEX_NAMES


def test_public_schema_descriptor_is_frozen_and_stable() -> None:
    schema_type = tinkerfin_sandbox.SQLAlchemyOpenSandboxStateSchema
    assert isinstance(schema_type, type), "Schema descriptor must be public"
    assert is_dataclass(schema_type)
    assert tuple(field.name for field in fields(schema_type)) == (
        "dialect",
        "table_names",
        "ddl",
    )

    first = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    second = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect="mysql")

    assert first == second
    assert first is not second
    assert first.dialect == "mysql"
    assert first.table_names == _TABLE_NAMES
    with pytest.raises(FrozenInstanceError):
        setattr(first, "dialect", "sqlite")


@pytest.mark.parametrize(
    ("dialect", "digest"),
    [
        (
            "mysql",
            "b1f087c300aee2ac9a9d20aa69f2873bcf1242e28e2ad7cc7366d8b3c09cd086",
        ),
        (
            "sqlite",
            "c1e0868f9ce6f99f4955be2e23af09e31a37c082b87899e9ca986b3a3b1b06b7",
        ),
    ],
)
def test_schema_descriptor_has_exact_current_ddl(
    dialect: Literal["mysql", "sqlite"],
    digest: str,
) -> None:
    schema = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect=dialect)

    assert sha256(schema.ddl.encode()).hexdigest() == digest


def test_schema_generation_does_not_create_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"engine created with {args!r} and {kwargs!r}")

    monkeypatch.setattr(
        sqlalchemy_lifecycle,
        "create_async_engine",
        unexpected_engine,
    )

    assert (
        tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(
            dialect="sqlite"
        ).dialect
        == "sqlite"
    )


def test_sqlalchemy_exports_remain_optional_and_lazy() -> None:
    script = """
import builtins
import sys

import tinkerfin_sandbox

assert 'tinkerfin_sandbox.lifecycle.sqlalchemy' not in sys.modules
original_import = builtins.__import__

def block_sqlalchemy(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'sqlalchemy' or name.startswith('sqlalchemy.'):
        error = ModuleNotFoundError("blocked optional SQLAlchemy dependency")
        error.name = name
        raise error
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = block_sqlalchemy
try:
    getattr(tinkerfin_sandbox, 'get_sqlalchemy_opensandbox_state_schema')
except ImportError as error:
    message = str(error)
    assert 'tinkerfin-sandbox[sqlite]' in message
    assert 'tinkerfin-sandbox[mysql]' in message
else:
    raise AssertionError('optional SQLAlchemy export unexpectedly loaded')
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_schema_generator_rejects_an_unknown_dialect() -> None:
    invalid = cast(Literal["mysql", "sqlite"], "postgresql")

    with pytest.raises(ValueError, match="mysql.*sqlite"):
        tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect=invalid)


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_schema_ddl_has_complete_deterministic_statement_order(
    dialect: Literal["mysql", "sqlite"],
) -> None:
    schema = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect=dialect)
    statements = tuple(statement.strip() for statement in schema.ddl.split(";\n\n"))

    assert schema.ddl.endswith(";\n")
    assert len(statements) == 8
    assert tuple(statement.split("\n", 1)[0] for statement in statements[:4]) == (
        "CREATE TABLE tinkerfin_opensandbox_cleanup (",
        "CREATE TABLE tinkerfin_opensandbox_owners (",
        "CREATE TABLE tinkerfin_opensandbox_warm_slots (",
        "CREATE TABLE tinkerfin_opensandbox_workers (",
    )
    assert (
        tuple(statement.split(" ", 3)[2] for statement in statements[4:])
        == _INDEX_NAMES
    )
    assert "tinkerfin_opensandbox_schema_versions" not in schema.ddl


def test_mysql_ddl_uses_only_the_common_mysql_57_contract() -> None:
    ddl = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect="mysql").ddl

    assert ddl.count(" COMMENT ") == 28
    assert ddl.count(")COMMENT='") == 4
    assert "schema version" not in ddl
    assert "COMMENT 'UTC expiry of the current cleanup lease'" in ddl
    assert "IF NOT EXISTS" not in ddl
    assert "COLLATE" not in ddl
    assert "INVISIBLE" not in ddl
    assert "SKIP LOCKED" not in ddl


def test_sqlite_ddl_does_not_claim_to_persist_comments() -> None:
    ddl = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(
        dialect="sqlite"
    ).ddl

    assert "COMMENT" not in ddl
    assert "IF NOT EXISTS" not in ddl


async def test_sqlite_export_initializes_a_runtime_compatible_empty_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "exported-schema.db"
    schema = tinkerfin_sandbox.get_sqlalchemy_opensandbox_state_schema(dialect="sqlite")
    async with aiosqlite.connect(database_path) as connection:
        await connection.executescript(schema.ddl)
        await connection.commit()

    state_type = tinkerfin_sandbox.SQLAlchemyOpenSandboxState
    state = state_type(url=_sqlite_url(database_path), namespace="test")
    await state.start(warm_pool_size=0)
    await state.aclose()

    assert await _sqlite_schema_objects(database_path, kind="table") == _TABLE_NAMES
    assert await _sqlite_schema_objects(database_path, kind="index") == _INDEX_NAMES


@pytest.mark.parametrize(
    "mutation",
    (
        "CREATE UNIQUE INDEX unexpected_owner_namespace "
        "ON tinkerfin_opensandbox_owners(namespace);",
        "DROP INDEX ix_tinkerfin_opensandbox_owners_lease; "
        "CREATE UNIQUE INDEX ix_tinkerfin_opensandbox_owners_lease "
        "ON tinkerfin_opensandbox_owners(namespace, lease_expires_at);",
    ),
)
async def test_sqlite_start_rejects_indexes_outside_the_current_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    database_path = tmp_path / "incompatible-index.db"
    state_type = tinkerfin_sandbox.SQLAlchemyOpenSandboxState
    initial = state_type(url=_sqlite_url(database_path), namespace="test")
    await initial.start(warm_pool_size=0)
    await initial.aclose()
    async with aiosqlite.connect(database_path) as connection:
        await connection.executescript(mutation)
        await connection.commit()

    candidate = state_type(url=_sqlite_url(database_path), namespace="test")
    try:
        with pytest.raises(
            tinkerfin_sandbox.OpenSandboxStateError,
            match="schema is incompatible",
        ):
            await candidate.start(warm_pool_size=0)
    finally:
        await candidate.aclose()


@pytest.mark.parametrize(
    ("dialect_name", "server_version", "supports_skip_locked"),
    [
        ("sqlite", (3, 49, 1), False),
        ("mysql", (8, 0, 41), True),
        ("mysql", (8, 4, 6), True),
    ],
)
def test_runtime_dialect_capabilities_distinguish_supported_servers(
    dialect_name: str,
    server_version: tuple[int, ...],
    supports_skip_locked: bool,
) -> None:
    capabilities = sqlalchemy_lifecycle._resolve_dialect_capabilities(
        dialect_name=dialect_name,
        server_version=server_version,
        is_mariadb=False,
    )

    assert capabilities.name == dialect_name
    assert capabilities.server_version == server_version
    assert capabilities.supports_skip_locked is supports_skip_locked
    with pytest.raises(FrozenInstanceError):
        setattr(capabilities, "supports_skip_locked", not supports_skip_locked)


@pytest.mark.parametrize(
    ("dialect_name", "server_version", "is_mariadb", "match"),
    [
        ("mysql", (5, 6, 51), False, "supports MySQL"),
        ("mysql", (9, 0, 0), False, "supports MySQL"),
        ("mysql", (10, 11, 0), True, "MariaDB"),
        ("sqlite", (), False, "version"),
    ],
)
def test_runtime_dialect_capabilities_reject_unverified_servers(
    dialect_name: str,
    server_version: tuple[int, ...],
    is_mariadb: bool,
    match: str,
) -> None:
    configuration_error = tinkerfin_sandbox.OpenSandboxStateConfigurationError

    with pytest.raises(configuration_error, match=match):
        sqlalchemy_lifecycle._resolve_dialect_capabilities(
            dialect_name=dialect_name,
            server_version=server_version,
            is_mariadb=is_mariadb,
        )


@pytest.mark.parametrize(
    "statement",
    [
        select(sqlalchemy_lifecycle._warm_slots),
        select(
            sqlalchemy_lifecycle._warm_slots.c.slot,
            sqlalchemy_lifecycle._warm_slots.c.sandbox_id,
        ),
        select(sqlalchemy_lifecycle._cleanup),
    ],
)
def test_mysql8_claim_lock_sql_uses_skip_locked(
    statement: Select[tuple[object, ...]],
) -> None:
    mysql8 = sqlalchemy_lifecycle._resolve_dialect_capabilities(
        dialect_name="mysql",
        server_version=(8, 0, 41),
        is_mariadb=False,
    )
    compiler = mysql_dialect.dialect()

    mysql8_sql = str(
        sqlalchemy_lifecycle._apply_claim_lock(
            statement,
            capabilities=mysql8,
        ).compile(dialect=compiler)
    )

    assert mysql8_sql.endswith("FOR UPDATE SKIP LOCKED")
