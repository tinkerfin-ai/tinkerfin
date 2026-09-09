"""Explicit batches obey the same identity and persistence limits as Store calls."""

from __future__ import annotations

import asyncio

import pytest
from langgraph.store.base import (
    GetOp,
    InvalidNamespaceError,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    SearchItem,
    SearchOp,
)
from langgraph.store.memory import InMemoryStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_langgraph_mysql import AsyncMyStore, LangGraphMySQLSchemaError

pytestmark = [pytest.mark.docker_integration, pytest.mark.mysql_integration]


async def test_explicit_batches_keep_exact_identities_and_legal_namespace_matching(
    mysql_sandbox_url: str,
) -> None:
    namespaces = (
        ("scope", "*"),
        ("scope", "%_"),
        ("scope", "*", "leaf"),
        ("scope", "普通🙂 "),
    )
    memory = InMemoryStore()
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        writes = [
            PutOp(namespace, "é ", {"index": index})
            for index, namespace in enumerate(namespaces)
        ]
        assert await store.abatch(iter(writes)) == await memory.abatch(writes)
        reads: list[Op] = [
            GetOp(namespaces[-1], "é "),
            SearchOp(("scope", "*")),
            ListNamespacesOp(
                match_conditions=(MatchCondition("prefix", ("scope", "*")),)
            ),
            SearchOp(()),
        ]
        actual = await store.abatch(iter(reads))
        expected = await memory.abatch(reads)
        assert len(actual) == len(expected) == len(reads)
        for actual_result, expected_result in zip(actual, expected, strict=True):
            if isinstance(expected_result, Item):
                assert isinstance(actual_result, Item)
                assert (
                    actual_result.namespace,
                    actual_result.key,
                    actual_result.value,
                ) == (
                    expected_result.namespace,
                    expected_result.key,
                    expected_result.value,
                )
            else:
                assert isinstance(actual_result, list)
                assert isinstance(expected_result, list)
                assert {
                    (item.namespace, item.key) if isinstance(item, SearchItem) else item
                    for item in actual_result
                } == {
                    (item.namespace, item.key) if isinstance(item, SearchItem) else item
                    for item in expected_result
                }


@pytest.mark.parametrize("synchronous", (False, True))
async def test_explicit_batch_rejects_ttl_before_accepting_any_write(
    mysql_sandbox_url: str, synchronous: bool
) -> None:
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        operations = [
            PutOp(("scope",), "first", {"value": "must not be committed"}),
            PutOp(("scope",), "expiring", {"value": "temporary"}, ttl=1),
        ]
        with pytest.raises(NotImplementedError, match="TTL"):
            if synchronous:
                await asyncio.to_thread(store.batch, operations)
            else:
                await store.abatch(iter(operations))
        assert await store.asearch(("scope",)) == []


@pytest.mark.parametrize("operation", ("put", "get", "delete"))
async def test_explicit_document_operations_reject_ambiguous_namespace_labels(
    mysql_sandbox_url: str, operation: str
) -> None:
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        await store.aput(("tenants", "Alice"), "memory", {"owner": "Alice"})
        namespace = ("tenants.Alice",)
        op = (
            GetOp(namespace, "memory")
            if operation == "get"
            else PutOp(
                namespace,
                "memory",
                {"owner": "other"} if operation == "put" else None,
            )
        )
        with pytest.raises(InvalidNamespaceError):
            await store.abatch([op])
        original = await store.aget(("tenants", "Alice"), "memory")
        assert original is not None and original.value == {"owner": "Alice"}


async def test_startup_rejects_a_primary_key_that_truncates_document_identity(
    mysql_sandbox_url: str,
) -> None:
    async with AsyncMyStore.from_conn_string(mysql_sandbox_url) as store:
        await store.aput(("scope",), "first", {"owner": "original"})
    engine = create_async_engine(mysql_sandbox_url)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE store DROP PRIMARY KEY, ADD PRIMARY KEY (prefix(1), `key`(1))"
            )
        with pytest.raises(LangGraphMySQLSchemaError):
            async with AsyncMyStore.from_conn_string(mysql_sandbox_url):
                pytest.fail("an unsafe Store schema was accepted")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT COUNT(*) FROM store")) == 1
    finally:
        await engine.dispose()
