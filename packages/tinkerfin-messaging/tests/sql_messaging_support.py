"""Own disposable Engines borrowed by Messaging contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.support.docker_services import MySQLTestService, PostgreSQLTestService


@asynccontextmanager
async def messaging_engine(
    family: str, request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[AsyncEngine]:
    if family == "sqlite":
        instance = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'messaging.db'}",
            pool_size=4,
            max_overflow=0,
        )
        try:
            async with instance.begin() as connection:
                await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            yield instance
        finally:
            await instance.dispose()
        return
    name = f"messaging_test_{uuid4().hex}"
    service = request.getfixturevalue(f"{family}_test_service")
    if isinstance(service, MySQLTestService):
        admin = create_async_engine(service.url("tinkerfin_test_admin"))
        async with admin.begin() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
            )
        instance = create_async_engine(
            service.url(name),
            pool_size=4,
            max_overflow=0,
            connect_args={"init_command": "SET time_zone = '+08:00'"},
        )
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
