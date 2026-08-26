"""Pure contracts for repository Docker service descriptors."""

from collections.abc import Awaitable
from typing import cast

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support.docker_services import MySQLTestService, RedisTestService


def test_redis_service_selects_one_explicit_logical_database() -> None:
    service = RedisTestService(host="127.0.0.1", port=12345)

    assert service.url(0) == "redis://127.0.0.1:12345/0"
    assert service.url(15) == "redis://127.0.0.1:12345/15"


def test_mysql_service_builds_an_encoded_async_url() -> None:
    service = MySQLTestService(
        host="127.0.0.1",
        port=12346,
        password="secret:/ value",
    )

    url = make_url(service.url("sandbox_test"))

    assert url.drivername == "mysql+asyncmy"
    assert url.username == "root"
    assert url.password == "secret:/ value"
    assert url.host == "127.0.0.1"
    assert url.port == 12346
    assert url.database == "sandbox_test"
    assert url.query == {"charset": "utf8mb4"}


@pytest.mark.docker_integration
async def test_redis_stack_fixture_exposes_ordinary_and_search_databases(
    redis_url: str,
    redis_checkpoint_url: str,
) -> None:
    ordinary = Redis.from_url(redis_url, decode_responses=True)
    checkpoint = Redis.from_url(redis_checkpoint_url, decode_responses=True)
    try:
        assert await cast(Awaitable[bool], ordinary.ping()) is True
        assert isinstance(await checkpoint.execute_command("FT._LIST"), list)
    finally:
        await ordinary.aclose()
        await checkpoint.aclose()


@pytest.mark.docker_integration
async def test_mysql_fixture_runs_the_deployed_server_line(
    mysql_admin_url: str,
) -> None:
    engine = create_async_engine(mysql_admin_url)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("SELECT VERSION()"))
    finally:
        await engine.dispose()

    assert isinstance(version, str)
    assert version.startswith("8.4.")
