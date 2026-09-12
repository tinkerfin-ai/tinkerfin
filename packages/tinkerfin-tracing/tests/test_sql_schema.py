"""Deterministic SQL Trace DDL ownership and index contracts."""

from __future__ import annotations

from typing import Literal

import pytest

from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES, get_trace_store_schema


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_trace_schema_contains_exact_owned_tables_without_foreign_keys(
    dialect: Literal["sqlite", "mysql", "postgresql"],
) -> None:
    schema = get_trace_store_schema(dialect=dialect)

    assert schema.table_names == TRACE_TABLE_NAMES
    assert all(name in schema.ddl for name in TRACE_TABLE_NAMES)
    assert "FOREIGN KEY" not in schema.ddl.upper()
    assert "schema_version" not in schema.ddl.lower()
    assert "payload_digest" in schema.ddl
    assert "state_digest" in schema.ddl


def test_mysql_schema_preserves_comments_and_uses_digest_keys_for_long_ids() -> None:
    schema = get_trace_store_schema(dialect="mysql")

    assert "COMMENT" in schema.ddl
    assert "LONGBLOB" in schema.ddl
    assert "DATETIME(6)" in schema.ddl
    assert "namespace_hash" in schema.ddl
    assert "thread_hash" in schema.ddl
    assert "run_hash" in schema.ddl
    assert "terminal_committed" in schema.ddl
    assert "closed_committed" in schema.ddl


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_trace_schema_compilation_is_byte_deterministic(
    dialect: Literal["sqlite", "mysql", "postgresql"],
) -> None:
    assert (
        get_trace_store_schema(dialect=dialect).ddl
        == get_trace_store_schema(dialect=dialect).ddl
    )


def test_postgresql_ddl_includes_every_metadata_comment() -> None:
    from tinkerfin_tracing.sql_schema import metadata

    ddl = get_trace_store_schema(dialect="postgresql").ddl
    assert ddl.count("COMMENT ON TABLE") == sum(
        table.comment is not None for table in metadata.tables.values()
    )
    assert ddl.count("COMMENT ON COLUMN") == sum(
        column.comment is not None
        for table in metadata.tables.values()
        for column in table.columns
    )
    assert "BYTEA" in ddl
    assert "TIMESTAMP WITHOUT TIME ZONE" in ddl
