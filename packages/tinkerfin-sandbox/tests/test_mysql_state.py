from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_sandbox import (
    SQLAlchemyOpenSandboxState,
    get_sqlalchemy_opensandbox_state_schema,
)


@dataclass(frozen=True, slots=True)
class _MySQLServer:
    url: str


def _configured_mysql8_server() -> _MySQLServer | None:
    mysql8_url = os.environ.get("TINKERFIN_TEST_MYSQL8_URL")
    return None if not mysql8_url else _MySQLServer(url=mysql8_url)


_MYSQL8_SERVER = _configured_mysql8_server()
_MYSQL_PARAMS = (
    (pytest.param(_MYSQL8_SERVER, id="mysql8"),)
    if _MYSQL8_SERVER is not None
    else (
        pytest.param(
            None,
            marks=pytest.mark.skip(
                reason="a disposable MySQL 8 URL was not configured"
            ),
            id="unconfigured",
        ),
    )
)


def _ddl_statements(ddl: str) -> tuple[str, ...]:
    return tuple(
        statement.strip().removesuffix(";")
        for statement in ddl.split(";\n\n")
        if statement.strip()
    )


def _reflect_mysql_schema(
    connection: Connection,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    inspector = inspect(connection)
    table_names = tuple(
        sorted(
            name
            for name in inspector.get_table_names()
            if name.startswith("tinkerfin_opensandbox_")
        )
    )
    index_names = tuple(
        sorted(
            str(index["name"])
            for table_name in table_names
            for index in inspector.get_indexes(table_name)
            if index.get("name") is not None
        )
    )
    table_comments = tuple(
        str(inspector.get_table_comment(table_name).get("text") or "")
        for table_name in table_names
    )
    column_comments = tuple(
        str(column.get("comment") or "")
        for table_name in table_names
        for column in inspector.get_columns(table_name)
    )
    return table_names, index_names, table_comments, column_comments


async def _reset_and_apply_exported_schema(server: _MySQLServer) -> None:
    schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    engine = create_async_engine(server.url)
    try:
        async with engine.begin() as connection:
            for table_name in reversed(schema.table_names):
                await connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{table_name}`")
            for statement in _ddl_statements(schema.ddl):
                await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@pytest.mark.mysql_integration
@pytest.mark.parametrize("server", _MYSQL_PARAMS)
async def test_mysql8_export_and_runtime_claims_are_compatible(
    server: _MySQLServer | None,
) -> None:
    assert server is not None
    await _reset_and_apply_exported_schema(server)
    observed_sql: list[str] = []
    first = SQLAlchemyOpenSandboxState(
        url=server.url,
        namespace="integration-mysql8",
        lease_ttl=1.0,
        poll_interval=0.01,
        sqlite_retry_timeout=0,
    )
    second = SQLAlchemyOpenSandboxState(
        url=server.url,
        namespace="integration-mysql8",
        lease_ttl=1.0,
        poll_interval=0.01,
        sqlite_retry_timeout=0,
    )

    def capture_sql(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        observed_sql.append(statement.upper())

    event.listen(first._engine.sync_engine, "before_cursor_execute", capture_sql)
    event.listen(second._engine.sync_engine, "before_cursor_execute", capture_sql)
    try:
        await asyncio.gather(
            first.start(warm_pool_size=2),
            second.start(warm_pool_size=2),
        )
        capabilities = first._require_capabilities()
        assert capabilities.server_version[:2] == (8, 0)
        assert capabilities.supports_skip_locked is True

        initial_owner = await first.acquire_owner("serialized-owner")
        waiting_owner = asyncio.create_task(second.acquire_owner("serialized-owner"))
        await asyncio.sleep(0.05)
        assert not waiting_owner.done()
        await first.release_owner(initial_owner)
        successor_owner = await asyncio.wait_for(waiting_owner, timeout=2)
        assert successor_owner.generation > initial_owner.generation
        await second.release_owner(successor_owner)

        warm_claims = await asyncio.gather(
            first.claim_warm_slot(),
            second.claim_warm_slot(),
        )
        assert all(claim is not None for claim in warm_claims)
        first_warm = warm_claims[0]
        second_warm = warm_claims[1]
        assert first_warm is not None
        assert second_warm is not None
        assert first_warm.slot != second_warm.slot
        await asyncio.gather(
            first.publish_warm(first_warm, "warm-a"),
            second.publish_warm(second_warm, "warm-b"),
        )

        first_owner, second_owner = await asyncio.gather(
            first.acquire_owner("owner-a"),
            second.acquire_owner("owner-b"),
        )
        bindings = await asyncio.gather(
            first.consume_warm(first_owner),
            second.consume_warm(second_owner),
        )
        assert {binding.sandbox_id for binding in bindings if binding is not None} == {
            "warm-a",
            "warm-b",
        }
        assert await first.read_binding("owner-a") == bindings[0]
        assert await second.read_binding("owner-b") == bindings[1]
        await asyncio.gather(
            first.release_owner(first_owner),
            second.release_owner(second_owner),
        )

        await asyncio.gather(
            first.enqueue_cleanup("cleanup-a"),
            second.enqueue_cleanup("cleanup-b"),
        )
        concurrent_cleanup_claims = await asyncio.gather(
            first.claim_cleanup(),
            second.claim_cleanup(),
        )
        cleanup_claims = (
            concurrent_cleanup_claims[0] or await first.claim_cleanup(),
            concurrent_cleanup_claims[1] or await second.claim_cleanup(),
        )
        assert all(claim is not None for claim in cleanup_claims)
        claimed_cleanup_ids = {
            claim.sandbox_id for claim in cleanup_claims if claim is not None
        }
        assert claimed_cleanup_ids == {"cleanup-a", "cleanup-b"}
        await asyncio.gather(
            *(
                first.complete_cleanup(claim)
                for claim in cleanup_claims
                if claim is not None
            )
        )

        stale_warm = await first.claim_warm_slot()
        other_stale_warm = await first.claim_warm_slot()
        assert stale_warm is not None
        assert other_stale_warm is not None
        assert stale_warm.slot != other_stale_warm.slot
        await asyncio.sleep(1.7)
        replacement_warm = await second.claim_warm_slot()
        assert replacement_warm is not None
        assert replacement_warm.slot == min(stale_warm.slot, other_stale_warm.slot)
        prior_generation = (
            stale_warm.generation
            if replacement_warm.slot == stale_warm.slot
            else other_stale_warm.generation
        )
        assert replacement_warm.generation > prior_generation
        await second.release_warm(replacement_warm)
        await first.release_warm(stale_warm)
        await first.release_warm(other_stale_warm)

        await first.enqueue_cleanup("cleanup-expiring")
        stale_cleanup = await first.claim_cleanup()
        assert stale_cleanup is not None
        await asyncio.sleep(1.7)
        replacement_cleanup = await second.claim_cleanup()
        assert replacement_cleanup is not None
        assert replacement_cleanup.sandbox_id == stale_cleanup.sandbox_id
        assert replacement_cleanup.generation > stale_cleanup.generation
        await second.complete_cleanup(replacement_cleanup)
    finally:
        event.remove(first._engine.sync_engine, "before_cursor_execute", capture_sql)
        event.remove(second._engine.sync_engine, "before_cursor_execute", capture_sql)
        await asyncio.gather(first.aclose(), second.aclose())

    engine = create_async_engine(server.url)
    try:
        async with engine.connect() as connection:
            reflected = await connection.run_sync(_reflect_mysql_schema)
            version = await connection.scalar(
                text(
                    "SELECT version FROM tinkerfin_opensandbox_schema_versions "
                    "WHERE component = 'opensandbox-state'"
                )
            )
    finally:
        await engine.dispose()

    table_names, index_names, table_comments, column_comments = reflected
    schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    assert table_names == schema.table_names
    assert index_names == (
        "ix_tinkerfin_opensandbox_cleanup_lease",
        "ix_tinkerfin_opensandbox_owners_lease",
        "ix_tinkerfin_opensandbox_warm_slots_available",
        "ix_tinkerfin_opensandbox_workers_lease",
    )
    assert version == 2
    assert len(table_comments) == 5
    assert all(table_comments)
    assert len(column_comments) == 30
    assert all(column_comments)
    claim_sql = tuple(sql for sql in observed_sql if " FOR UPDATE" in sql)
    assert claim_sql
    assert any("TINKERFIN_OPENSANDBOX_WARM_SLOTS" in sql for sql in claim_sql)
    assert any("TINKERFIN_OPENSANDBOX_CLEANUP" in sql for sql in claim_sql)
    assert any("FOR UPDATE SKIP LOCKED" in sql for sql in claim_sql)
