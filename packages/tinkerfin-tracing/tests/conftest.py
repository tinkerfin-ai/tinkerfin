"""Fresh databases for one Trace contract across the three SQL dialects."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.docker_services import MySQLTestService, PostgreSQLTestService


@pytest.fixture
def trace_mysql_database(
    mysql_test_service: MySQLTestService,
) -> Callable[[], AbstractAsyncContextManager[str]]:
    """Allow conditional MySQL fixtures to enter database ownership asynchronously."""

    return partial(_mysql_database, mysql_test_service)


@pytest.fixture
async def trace_mysql_url(
    trace_mysql_database: Callable[[], AbstractAsyncContextManager[str]],
) -> AsyncIterator[str]:
    """Prepare a private database before a MySQL-only test begins."""

    async with trace_mysql_database() as url:
        yield url


@asynccontextmanager
async def _mysql_database(mysql_test_service: MySQLTestService) -> AsyncIterator[str]:
    """Give every MySQL Trace test a database that only that test may destroy."""

    name = f"trace_test_{uuid4().hex}"
    admin = create_async_engine(mysql_test_service.url("tinkerfin_test_admin"))
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            )
        try:
            yield mysql_test_service.url(name)
        finally:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP DATABASE `{name}`"))
    finally:
        await admin.dispose()


@pytest.fixture
def trace_postgresql_database(
    postgresql_test_service: PostgreSQLTestService,
) -> Callable[[], AbstractAsyncContextManager[AsyncEngine]]:
    """Enter a private PostgreSQL schema from conditional backend fixtures."""

    return partial(_postgresql_database, postgresql_test_service)


@asynccontextmanager
async def _postgresql_database(
    service: PostgreSQLTestService,
) -> AsyncIterator[AsyncEngine]:
    name = f"trace_test_{uuid4().hex}"
    admin = create_async_engine(service.url())
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{name}"'))
        instance = create_async_engine(
            service.url(),
            pool_size=4,
            max_overflow=0,
            connect_args={
                "server_settings": {"search_path": name, "timezone": "Asia/Shanghai"}
            },
        )
        try:
            yield instance
        finally:
            await instance.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
    finally:
        await admin.dispose()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    ]
)
async def trace_sql_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    family: str = request.param
    if family == "sqlite":
        instance = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'traces.db'}", pool_size=4, max_overflow=0
        )
        try:
            async with instance.begin() as connection:
                await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            yield instance
        finally:
            await instance.dispose()
        return
    name = f"trace_test_{uuid4().hex}"
    service = request.getfixturevalue(f"{family}_test_service")
    if isinstance(service, MySQLTestService):
        admin = create_async_engine(service.url("tinkerfin_test_admin"))
        async with admin.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            )
        instance = create_async_engine(service.url(name), pool_size=4, max_overflow=0)
        cleanup = f"DROP DATABASE `{name}`"
    else:
        assert isinstance(service, PostgreSQLTestService)
        admin = create_async_engine(service.url())
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{name}"'))
        instance = create_async_engine(
            service.url(),
            pool_size=4,
            max_overflow=0,
            connect_args={
                "server_settings": {"search_path": name, "timezone": "Asia/Shanghai"}
            },
        )
        cleanup = f'DROP SCHEMA "{name}" CASCADE'
    try:
        yield instance
    finally:
        await instance.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(cleanup))
        await admin.dispose()
