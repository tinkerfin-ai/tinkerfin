from __future__ import annotations

import asyncio
import re
import secrets
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import pytest
from asyncmy import Connection
from asyncmy.cursors import DictCursor
from asyncmy.errors import Error as AsyncMyError
from langgraph.store.base import GetOp
from langgraph.store.memory import InMemoryStore
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_langgraph_mysql import AsyncMyStore, LangGraphMySQLSchemaError

_DATABASE_PATTERN = re.compile(r"\Atinkerfin_store_[a-f0-9]{16}\Z")


class _RollbackFailingCursor:
    async def execute(self, _operation: str, _parameters: object = None) -> None:
        return None

    async def fetchall(self) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        return [
            {
                "key": "key",
                "value": b"{}",
                "prefix": "scope",
                "created_at": now,
                "updated_at": now,
            }
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _RollbackFailingConnection:
    def cursor(self, _cursor_type: object) -> _RollbackFailingCursor:
        return _RollbackFailingCursor()

    async def begin(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        raise AsyncMyError("rollback failed")


@pytest.mark.parametrize(
    "error",
    [
        SystemExit("stop"),
        KeyboardInterrupt("stop"),
        GeneratorExit("stop"),
        asyncio.CancelledError("stop"),
    ],
)
async def test_batch_preserves_process_control_when_rollback_fails(
    error: BaseException,
) -> None:
    """Rollback driver failure must remain secondary to process control."""

    def fail_deserialization(_payload: object) -> dict[str, Any]:
        raise error

    connection: Any = _RollbackFailingConnection()
    store = AsyncMyStore(connection, deserializer=fail_deserialization)

    with pytest.raises(type(error)) as captured:
        await store.abatch([GetOp(namespace=("scope",), key="key")])

    assert captured.value is error
    assert isinstance(captured.value.__cause__, AsyncMyError)


def test_connection_url_is_parsed_for_asyncmy() -> None:
    parsed = AsyncMyStore.parse_conn_string(
        "mysql+asyncmy://user:p%40ss@db.internal:3307/agents?unix_socket=%2Ftmp%2Fmysql.sock"
    )

    assert parsed == {
        "host": "db.internal",
        "user": "user",
        "password": "p@ss",
        "db": "agents",
        "port": 3307,
        "unix_socket": "/tmp/mysql.sock",
    }

    with pytest.raises(ValueError, match="mysql or mysql\\+asyncmy"):
        AsyncMyStore.parse_conn_string("postgresql://user:secret@db/agents")


async def _connection_count(engine: AsyncEngine, database_name: str) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM information_schema.processlist "
                "WHERE db = :database_name AND id <> CONNECTION_ID()"
            ),
            {"database_name": database_name},
        )
    if not isinstance(count, int):
        raise TypeError("MySQL did not return a connection count")
    return count


async def _wait_for_connection_count(
    engine: AsyncEngine,
    database_name: str,
    expected: int,
) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if await _connection_count(engine, database_name) == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"database connection count did not settle at {expected}: "
        f"{await _connection_count(engine, database_name)}"
    )


async def _store_table_names(engine: AsyncEngine, database_name: str) -> set[str]:
    """Read LangGraph Store table names from one disposable database."""

    async with engine.connect() as connection:
        rows = await connection.scalars(
            text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME LIKE 'store%'"
            ),
            {"database_name": database_name},
        )
        return set(rows)


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_asyncmy_store_round_trips_the_public_base_store_contract(
    mysql_admin_url: str,
) -> None:
    admin_url = make_url(mysql_admin_url)
    database_name = f"tinkerfin_store_{secrets.token_hex(8)}"
    if _DATABASE_PATTERN.fullmatch(database_name) is None:
        raise AssertionError("generated disposable database name is invalid")
    admin_engine = create_async_engine(admin_url)
    store_url = admin_url.set(database=database_name, drivername="mysql")
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
            )
        async with AsyncMyStore.from_conn_string(
            store_url.render_as_string(hide_password=False)
        ) as store:
            memory = InMemoryStore()
            assert await _connection_count(admin_engine, database_name) == 1
            await store.aput(("users", "42"), "preferences", {"theme": "dark"})
            await memory.aput(("users", "42"), "preferences", {"theme": "dark"})
            item = await store.aget(("users", "42"), "preferences")
            assert item is not None
            assert item.value == {"theme": "dark"}
            assert [result.key for result in await store.asearch(("users",))] == [
                "preferences"
            ]
            assert await store.alist_namespaces(prefix=("users",)) == [("users", "42")]

            namespace_items = (
                (("user",), "exact", {"kind": "exact"}),
                (("user", "percent%_"), "child", {"kind": "child"}),
                (("users",), "neighbor", {"kind": "neighbor"}),
                (("*",), "literal-star", {"kind": "literal-star"}),
                (("*", "child"), "literal-star-child", {"kind": "literal-star-child"}),
            )
            for namespace, key, value in namespace_items:
                await store.aput(namespace, key, value)
                await memory.aput(namespace, key, value)
            assert (
                {result.key for result in await store.asearch(("user",))}
                == {result.key for result in await memory.asearch(("user",))}
                == {
                    "exact",
                    "child",
                }
            )
            assert await store.alist_namespaces(
                prefix=("user",)
            ) == await memory.alist_namespaces(prefix=("user",))
            assert await store.alist_namespaces(
                suffix=("percent%_",)
            ) == await memory.alist_namespaces(suffix=("percent%_",))
            assert await store.alist_namespaces(
                prefix=("user", "*")
            ) == await memory.alist_namespaces(prefix=("user", "*"))
            assert await store.alist_namespaces(
                prefix=("user",),
                max_depth=1,
            ) == await memory.alist_namespaces(prefix=("user",), max_depth=1)
            assert {result.key for result in await store.asearch(("*",))} == {
                result.key for result in await memory.asearch(("*",))
            }

            numeric_values = {
                "negative": -1,
                "two": 2,
                "decimal": 4.99,
                "ten": 10,
                "below-float-integer-limit": 2**53 - 1,
                "float-integer-limit": 2**53,
                "above-float-integer-limit": 2**53 + 1,
                "signed-integer-maximum": 2**63 - 1,
                "signed-integer-minimum": -(2**63),
                "unsigned-integer-minimum": 2**63,
                "unsigned-integer-maximum": 2**64 - 1,
            }
            for key, score in numeric_values.items():
                value = {"score": score}
                await store.aput(("scores",), key, value)
                await memory.aput(("scores",), key, value)
            connection = store.conn
            assert isinstance(connection, Connection)
            async with connection.cursor(DictCursor) as cursor:
                await cursor.execute(
                    "SELECT JSON_TYPE(JSON_EXTRACT(value, '$.score')) AS score_type "
                    "FROM store WHERE prefix = %s AND `key` IN (%s, %s)",
                    (
                        "scores",
                        "unsigned-integer-minimum",
                        "unsigned-integer-maximum",
                    ),
                )
                unsigned_rows = await cursor.fetchall()
            assert {row["score_type"] for row in unsigned_rows} == {"UNSIGNED INTEGER"}
            for operand in (
                2,
                2**53,
                2**53 + 1,
                2**63 - 1,
                -(2**63),
                2**63,
                2**64 - 1,
            ):
                for operator in ("$gt", "$gte", "$lt", "$lte", "$eq", "$ne"):
                    expected = {
                        result.key
                        for result in await memory.asearch(
                            ("scores",),
                            filter={"score": {operator: operand}},
                            limit=100,
                        )
                    }
                    actual = {
                        result.key
                        for result in await store.asearch(
                            ("scores",),
                            filter={"score": {operator: operand}},
                            limit=100,
                        )
                    }
                    assert actual == expected, (operator, operand, actual, expected)

            await store.adelete(("users", "42"), "preferences")
            assert await store.aget(("users", "42"), "preferences") is None
        await _wait_for_connection_count(admin_engine, database_name, 0)
        assert await _store_table_names(admin_engine, database_name) == {"store"}
    finally:
        if _DATABASE_PATTERN.fullmatch(database_name) is not None:
            async with admin_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"DROP DATABASE IF EXISTS `{database_name}`"
                )
        await admin_engine.dispose()


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_owned_store_connection_closes_after_failure_and_cancellation(
    mysql_admin_url: str,
) -> None:
    admin_url = make_url(mysql_admin_url)
    database_name = f"tinkerfin_store_{secrets.token_hex(8)}"
    if _DATABASE_PATTERN.fullmatch(database_name) is None:
        raise AssertionError("generated disposable database name is invalid")
    admin_engine = create_async_engine(admin_url)
    store_url = admin_url.set(database=database_name, drivername="mysql+asyncmy")
    connection_url = store_url.render_as_string(hide_password=False)
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
            )

        with pytest.raises(RuntimeError, match="body failed"):
            async with AsyncMyStore.from_conn_string(connection_url):
                assert await _connection_count(admin_engine, database_name) == 1
                raise RuntimeError("body failed")
        await _wait_for_connection_count(admin_engine, database_name, 0)

        caller_driver_error = AsyncMyError("caller-owned failure")
        with pytest.raises(AsyncMyError) as captured:
            async with AsyncMyStore.from_conn_string(connection_url):
                raise caller_driver_error
        assert captured.value is caller_driver_error
        await _wait_for_connection_count(admin_engine, database_name, 0)

        entered = asyncio.Event()
        never = asyncio.Event()

        async def hold_store() -> None:
            async with AsyncMyStore.from_conn_string(connection_url):
                entered.set()
                await never.wait()

        task = asyncio.create_task(hold_store())
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await _connection_count(admin_engine, database_name) == 1
        task.cancel("caller abandoned the Store")
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_for_connection_count(admin_engine, database_name, 0)
    finally:
        if _DATABASE_PATTERN.fullmatch(database_name) is not None:
            async with admin_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"DROP DATABASE IF EXISTS `{database_name}`"
                )
        await admin_engine.dispose()


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_concurrent_setup_uses_one_current_schema_without_version_tables(
    mysql_admin_url: str,
) -> None:
    """Concurrent empty-database setup creates only the current Store table."""

    admin_url = make_url(mysql_admin_url)
    database_name = f"tinkerfin_store_{secrets.token_hex(8)}"
    if _DATABASE_PATTERN.fullmatch(database_name) is None:
        raise AssertionError("generated disposable database name is invalid")
    admin_engine = create_async_engine(admin_url)
    store_url = admin_url.set(database=database_name, drivername="mysql+asyncmy")
    connection_url = store_url.render_as_string(hide_password=False)
    entered = 0
    entered_lock = asyncio.Lock()
    both_entered = asyncio.Event()

    async def enter_store() -> None:
        nonlocal entered
        async with AsyncMyStore.from_conn_string(connection_url):
            async with entered_lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5)

    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
            )
        await asyncio.gather(enter_store(), enter_store())
        await _wait_for_connection_count(admin_engine, database_name, 0)
        assert await _store_table_names(admin_engine, database_name) == {"store"}
    finally:
        if _DATABASE_PATTERN.fullmatch(database_name) is not None:
            async with admin_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"DROP DATABASE IF EXISTS `{database_name}`"
                )
        await admin_engine.dispose()


@pytest.mark.docker_integration
@pytest.mark.mysql_integration
async def test_setup_rejects_an_incompatible_existing_store_table(
    mysql_admin_url: str,
) -> None:
    """An incompatible existing table fails closed without runtime migration."""

    admin_url = make_url(mysql_admin_url)
    database_name = f"tinkerfin_store_{secrets.token_hex(8)}"
    if _DATABASE_PATTERN.fullmatch(database_name) is None:
        raise AssertionError("generated disposable database name is invalid")
    admin_engine = create_async_engine(admin_url)
    schema_engine = create_async_engine(
        admin_url.set(database=database_name, drivername="mysql+asyncmy")
    )
    connection_url = admin_url.set(
        database=database_name,
        drivername="mysql+asyncmy",
    ).render_as_string(hide_password=False)
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
            )
        async with schema_engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE store (`key` VARCHAR(150) NOT NULL PRIMARY KEY)"
            )
        await schema_engine.dispose()

        with pytest.raises(
            LangGraphMySQLSchemaError,
            match="does not match the current contract",
        ):
            async with AsyncMyStore.from_conn_string(connection_url):
                raise AssertionError("incompatible Store unexpectedly opened")
        await _wait_for_connection_count(admin_engine, database_name, 0)
        assert await _store_table_names(admin_engine, database_name) == {"store"}
    finally:
        await schema_engine.dispose()
        if _DATABASE_PATTERN.fullmatch(database_name) is not None:
            async with admin_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"DROP DATABASE IF EXISTS `{database_name}`"
                )
        await admin_engine.dispose()
