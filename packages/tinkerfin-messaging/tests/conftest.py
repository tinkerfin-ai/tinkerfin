"""Cross-backend test fixtures with mandatory real Redis coverage."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable
from typing import cast
from uuid import uuid4

import pytest
from backend_harness import MessagingBackendHarness
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tinkerfin_messaging import MemoryBackend, RedisBackend


async def _delete_prefix(client: Redis, prefix: str) -> None:
    cursor = 0
    while True:
        cursor, keys = await client.scan(
            cursor=cursor,
            match=f"{prefix}:*",
            count=200,
        )
        if keys:
            await client.unlink(*keys)
        if cursor == 0:
            return


@pytest.fixture(
    params=(
        pytest.param("memory", id="memory"),
        pytest.param(
            "redis",
            marks=(pytest.mark.docker_integration, pytest.mark.redis_e2e),
            id="redis",
        ),
    )
)
async def messaging_backend(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[MessagingBackendHarness, None]:
    """Yield an isolated backend implementation for the shared runtime contract."""

    if request.param == "memory":
        yield MessagingBackendHarness(MemoryBackend())
        return

    redis_url = request.getfixturevalue("redis_url")
    assert isinstance(redis_url, str)
    client = Redis.from_url(
        redis_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    prefix = f"tfmsg:runtime-contract:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                "real Redis PING failed without exposing credentials: "
                f"{type(error).__name__}"
            )
        yield MessagingBackendHarness(
            RedisBackend(
                client,
                key_prefix=prefix,
                producer_lease_seconds=3,
                generation_cleanup_retry_seconds=0.02,
            )
        )
    finally:
        await _delete_prefix(client, prefix)
        await client.aclose()
