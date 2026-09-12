"""Test-owned connections for Sandbox State contract checks."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.docker_services import MySQLTestService, PostgreSQLTestService
from tests.support.sql_engines import SqlEngineFactory


@pytest.fixture
async def sql_engine() -> AsyncIterator[SqlEngineFactory]:
    factory = SqlEngineFactory()
    try:
        yield factory
    finally:
        await factory.aclose()


@pytest.fixture
async def mysql57_sandbox_url(
    mysql57_test_service: MySQLTestService,
) -> AsyncIterator[str]:
    name = f"sandbox_test_{uuid4().hex}"
    admin = create_async_engine(mysql57_test_service.url("tinkerfin_test_admin"))
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            )
        yield mysql57_test_service.url(name)
    finally:
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        await admin.dispose()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    ]
)
async def sandbox_sql_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    family: str = request.param
    if family == "sqlite":
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'state.db'}", pool_size=4, max_overflow=0
        )
        try:
            yield engine
        finally:
            await engine.dispose()
        return
    name = f"sandbox_test_{uuid4().hex}"
    service = request.getfixturevalue(f"{family}_test_service")
    if isinstance(service, MySQLTestService):
        admin = create_async_engine(service.url("tinkerfin_test_admin"))
        async with admin.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            )
        engine = create_async_engine(service.url(name), pool_size=4, max_overflow=0)
        cleanup = f"DROP DATABASE `{name}`"
    else:
        assert isinstance(service, PostgreSQLTestService)
        admin = create_async_engine(service.url())
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{name}"'))
        engine = create_async_engine(
            service.url(),
            pool_size=4,
            max_overflow=0,
            connect_args={
                "server_settings": {"search_path": name, "timezone": "Asia/Shanghai"}
            },
        )
        cleanup = f'DROP SCHEMA "{name}" CASCADE'
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(cleanup))
        await admin.dispose()
