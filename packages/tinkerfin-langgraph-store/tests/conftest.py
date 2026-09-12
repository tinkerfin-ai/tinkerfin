"""Disposable databases for the same public Store contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.docker_services import (
    MySQLTestService,
    PostgreSQLTestService,
)

from tinkerfin_langgraph_store import SqlAlchemyStore


@pytest_asyncio.fixture(
    params=[
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
        pytest.param("postgresql", marks=pytest.mark.docker_integration),
    ]
)
async def storage(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    family: str = request.param
    if family == "sqlite":
        instance = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}", pool_size=4, max_overflow=0
        )
        try:
            async with instance.begin() as connection:
                await connection.exec_driver_sql("PRAGMA journal_mode = WAL")
            yield instance
        finally:
            await instance.dispose()
        return
    name = f"tinkerfin_store_test_{uuid4().hex}"
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
            connect_args={"server_settings": {"search_path": name}},
        )
        cleanup = f'DROP SCHEMA "{name}" CASCADE'
    try:
        yield instance
    finally:
        await instance.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(cleanup))
        await admin.dispose()


@pytest_asyncio.fixture
async def engine(
    storage: AsyncEngine,
) -> AsyncEngine:
    return storage


@pytest_asyncio.fixture
async def store(
    storage: AsyncEngine,
) -> AsyncIterator[SqlAlchemyStore]:
    instance = SqlAlchemyStore(storage)
    try:
        yield instance
    finally:
        await instance.aclose()
