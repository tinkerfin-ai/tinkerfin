"""Exact document identity and deterministic Store resource ownership."""

from __future__ import annotations

import asyncio

import pytest
from asyncmy import Connection, connect, create_pool
from asyncmy.cursors import DictCursor
from langgraph.store.memory import InMemoryStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_langgraph_mysql import (
    AsyncMyStore,
    LangGraphMySQLSchemaError,
    LangGraphMySQLStoreClosedError,
)

pytestmark = [pytest.mark.docker_integration, pytest.mark.mysql_integration]
_LABELS = ("Alice", "alice", "résumé", "resume", "é", "e\u0301", "owner", "owner ")


@pytest.mark.parametrize("identity_part", ("namespace", "key"))
async def test_store_preserves_exact_identities_like_memory(
    mysql_sandbox_url: str, identity_part: str
) -> None:
    memory = InMemoryStore()
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        entries = [
            (("tenants", label), "document")
            if identity_part == "namespace"
            else (("documents",), label)
            for label in _LABELS
        ]
        for label, (namespace, key) in zip(_LABELS, entries, strict=True):
            await store.aput(namespace, key, {"owner": label})
            await memory.aput(namespace, key, {"owner": label})
        for namespace, key in entries:
            actual = await store.aget(namespace, key)
            expected = await memory.aget(namespace, key)
            assert actual is not None and expected is not None
            assert (actual.namespace, actual.key, actual.value) == (
                expected.namespace,
                expected.key,
                expected.value,
            )
            assert {
                (item.namespace, item.key) for item in await store.asearch(namespace)
            } == {
                (item.namespace, item.key) for item in await memory.asearch(namespace)
            }
        assert set(await store.alist_namespaces()) == set(
            await memory.alist_namespaces()
        )
        namespace, key = entries[0]
        await store.adelete(namespace, key)
        assert await store.aget(namespace, key) is None
        sibling_namespace, sibling_key = entries[1]
        sibling = await store.aget(sibling_namespace, sibling_key)
        assert sibling is not None and sibling.value == {"owner": _LABELS[1]}


async def test_owned_context_leaves_no_running_store_work(
    mysql_sandbox_url: str,
) -> None:
    before = asyncio.all_tasks()
    remaining: set[asyncio.Task[object]] = set()
    try:
        async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
            await store.aput(("scope",), "key", {"value": "kept"})
        await asyncio.sleep(0)
        remaining = asyncio.all_tasks() - before
        assert not remaining
        with pytest.raises(LangGraphMySQLStoreClosedError):
            await store.aget(("scope",), "key")
    finally:
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def test_borrowed_store_close_keeps_the_driver_connection_usable(
    mysql_sandbox_url: str,
) -> None:
    async with connect(
        **AsyncMyStore.parse_conn_string(mysql_sandbox_url), autocommit=True
    ) as connection:
        assert isinstance(connection, Connection)
        store = AsyncMyStore(connection)
        await store.setup()
        await asyncio.to_thread(store.put, ("scope",), "key", {"value": "thread"})
        item = await asyncio.to_thread(store.get, ("scope",), "key")
        assert item is not None and item.value == {"value": "thread"}
        with pytest.raises(NotImplementedError, match="TTL"):
            await store.aput(("scope",), "unsupported-ttl", {}, ttl=1)
        assert await store.aget(("scope",), "unsupported-ttl") is None
        with pytest.raises(asyncio.InvalidStateError):
            store.get(("scope",), "key")
        await store.aclose()
        await store.aclose()
        with pytest.raises(LangGraphMySQLStoreClosedError):
            await store.setup()
        with pytest.raises(LangGraphMySQLStoreClosedError):
            await asyncio.to_thread(store.get, ("scope",), "key")
        async with connection.cursor(DictCursor) as cursor:
            await cursor.execute("SELECT 1 AS usable")
            assert await cursor.fetchone() == {"usable": 1}


async def _wait_for_query(
    engine: AsyncEngine, connection_id: int, fragment: str
) -> None:
    async with asyncio.timeout(2):
        while True:
            async with engine.connect() as connection:
                pending = await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.PROCESSLIST "
                        "WHERE ID = :id AND INFO LIKE :query"
                    ),
                    {"id": connection_id, "query": f"%{fragment}%"},
                )
            if pending == 1:
                return
            await asyncio.sleep(0.01)


async def test_borrowed_pool_close_settles_active_and_waiting_batches(
    mysql_sandbox_url: str,
) -> None:
    engine = create_async_engine(mysql_sandbox_url)
    try:
        async with create_pool(
            **AsyncMyStore.parse_conn_string(mysql_sandbox_url),
            autocommit=True,
            minsize=1,
            maxsize=1,
        ) as pool:
            store = AsyncMyStore(pool)
            await store.setup()
            async with pool.acquire() as driver, driver.cursor(DictCursor) as cursor:
                await cursor.execute("SELECT CONNECTION_ID() AS id")
                row = await cursor.fetchone()
                assert row is not None and isinstance(row["id"], int)
                connection_id = row["id"]
            async with engine.connect() as blocker:
                pending: list[asyncio.Task[None]] = []
                closing: asyncio.Task[None] | None = None
                try:
                    await blocker.exec_driver_sql("LOCK TABLES store WRITE")
                    pending = [
                        asyncio.create_task(store.aput(("scope",), key, {"key": key}))
                        for key in ("active", "waiting")
                    ]
                    await _wait_for_query(engine, connection_id, "INSERT INTO store")
                    closing = asyncio.create_task(store.aclose())
                    await asyncio.sleep(0)
                    assert not closing.done()
                    closing.cancel("first cancellation")
                    await asyncio.sleep(0)
                    closing.cancel("second cancellation")
                    await asyncio.sleep(0)
                    assert not closing.done()
                finally:
                    await blocker.exec_driver_sql("UNLOCK TABLES")
                    await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
                    if closing is not None:
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(closing, timeout=2)
                    await store.aclose()
            async with pool.acquire() as driver, driver.cursor(DictCursor) as cursor:
                await cursor.execute("SELECT `key` FROM store ORDER BY `key`")
                assert await cursor.fetchall() == [
                    {"key": "active"},
                    {"key": "waiting"},
                ]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("cancel_close", (False, True))
async def test_owned_close_settles_an_accepted_write_before_releasing_connection(
    mysql_sandbox_url: str, cancel_close: bool
) -> None:
    engine = create_async_engine(mysql_sandbox_url)
    context = AsyncMyStore.from_conn_string(mysql_sandbox_url)
    store = await context.__aenter__()
    driver = store.conn
    assert isinstance(driver, Connection)
    async with driver.cursor(DictCursor) as cursor:
        await cursor.execute("SELECT CONNECTION_ID() AS id")
        row = await cursor.fetchone()
        assert row is not None and isinstance(row["id"], int)
        connection_id = row["id"]
    blocker = await engine.connect()
    pending: asyncio.Task[None] | None = None
    closing: asyncio.Task[bool | None] | None = None
    locked = False
    try:
        await blocker.exec_driver_sql("LOCK TABLES store WRITE")
        locked = True
        pending = asyncio.create_task(store.aput(("scope",), "key", {"value": "kept"}))
        await _wait_for_query(engine, connection_id, "INSERT INTO store")
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0.02)
        assert not closing.done()
        if cancel_close:
            closing.cancel("close waiter cancelled")
            await asyncio.sleep(0)
            closing.cancel("close waiter cancelled again")
        await blocker.exec_driver_sql("UNLOCK TABLES")
        locked = False
        await asyncio.wait_for(pending, timeout=2)
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, timeout=2)
        else:
            await asyncio.wait_for(closing, timeout=2)
        async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as restored:
            item = await restored.aget(("scope",), "key")
            assert item is not None and item.value == {"value": "kept"}
    finally:
        if locked:
            await blocker.exec_driver_sql("UNLOCK TABLES")
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await context.__aexit__(None, None, None)
        await blocker.close()
        await engine.dispose()


async def test_schema_rejects_inexact_identity_collation_without_changing_data(
    mysql_sandbox_url: str,
) -> None:
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        await store.aput(("tenants", "Alice"), "memory", {"owner": "Alice"})
    engine = create_async_engine(mysql_sandbox_url)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE store MODIFY prefix VARCHAR(500) CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT 'Store document namespace'"
            )
        with pytest.raises(LangGraphMySQLSchemaError):
            async with AsyncMyStore.from_conn_string(mysql_sandbox_url):
                pytest.fail("inexact identity schema was accepted")
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE store MODIFY prefix VARCHAR(500) CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_0900_bin NOT NULL COMMENT 'Store document namespace'"
            )
        async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as restored:
            item = await restored.aget(("tenants", "Alice"), "memory")
            assert item is not None and item.value == {"owner": "Alice"}
    finally:
        await engine.dispose()


async def test_operation_timeout_settles_before_owned_connection_close(
    mysql_sandbox_url: str,
) -> None:
    engine = create_async_engine(mysql_sandbox_url)
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        driver = store.conn
        assert isinstance(driver, Connection)
        async with driver.cursor(DictCursor) as cursor:
            await cursor.execute("SELECT CONNECTION_ID() AS id")
            row = await cursor.fetchone()
            assert row is not None and isinstance(row["id"], int)
            connection_id = row["id"]
        blocker = await engine.connect()
        pending = None
        try:
            await blocker.exec_driver_sql("LOCK TABLES store WRITE")
            pending = asyncio.create_task(store.aget(("scope",), "key"))
            await _wait_for_query(engine, connection_id, "FROM store")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(pending, timeout=0.05)
            await asyncio.wait_for(store.aclose(), timeout=1)
        finally:
            await blocker.exec_driver_sql("UNLOCK TABLES")
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await blocker.close()
            await engine.dispose()
