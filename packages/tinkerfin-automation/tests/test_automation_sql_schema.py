from __future__ import annotations

from sqlalchemy import String

from tinkerfin_automation.sql_schema import (
    AUTOMATION_TABLE_NAMES,
    get_automation_store_schema,
    runs,
    scopes,
)


def test_schema_uses_exact_prefixed_table_set() -> None:
    assert AUTOMATION_TABLE_NAMES == (
        "tinkerfin_automation_tasks",
        "tinkerfin_automation_runs",
        "tinkerfin_automation_work_items",
        "tinkerfin_automation_scopes",
        "tinkerfin_automation_operations",
    )


def test_sqlite_and_mysql_compile_the_same_current_schema() -> None:
    sqlite = get_automation_store_schema("sqlite")
    mysql = get_automation_store_schema("mysql")

    assert sqlite.table_names == mysql.table_names == AUTOMATION_TABLE_NAMES
    assert len(sqlite.statements) == len(mysql.statements)
    sqlite_sql = "\n".join(sqlite.statements)
    mysql_sql = "\n".join(mysql.statements)
    for table_name in AUTOMATION_TABLE_NAMES:
        assert table_name in sqlite_sql
        assert table_name in mysql_sql
    assert "schema_version" not in sqlite_sql.lower()
    assert "schema_version" not in mysql_sql.lower()
    assert "uq_tinkerfin_automation_runs_occurrence" in sqlite_sql
    assert "uq_tinkerfin_automation_runs_identity" in mysql_sql
    assert "ix_tinkerfin_automation_work_available" in sqlite_sql
    assert "ix_tinkerfin_automation_work_lease" in mysql_sql
    assert runs.c.task_id.nullable is True
    assert isinstance(scopes.c.scope_key.type, String)
    assert scopes.c.scope_key.type.length == 191


def test_schema_rejects_unsupported_dialect() -> None:
    try:
        get_automation_store_schema("oracle")
    except ValueError as error:
        assert str(error) == "dialect must be 'sqlite', 'mysql', or 'postgresql'"
    else:  # pragma: no cover - assertion explains the expected contract
        raise AssertionError("unsupported dialect was accepted")
