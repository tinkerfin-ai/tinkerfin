"""Studio 外部依赖 readiness 检查"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tinkerfin_studio.config.settings import SandboxSettings
from tinkerfin_studio.infrastructure.database import Database


class ReadinessService:
    """并发检查请求链依赖且隐藏底层异常"""

    def __init__(
        self,
        *,
        database: Database,
        redis: Redis,
        sandbox: SandboxSettings,
        http_client: httpx.AsyncClient,
        timeout_seconds: float = 3,
    ) -> None:
        self._database = database
        self._redis = redis
        self._sandbox = sandbox
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    async def _check_mysql(self) -> None:
        async with self._database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def _check_redis(self) -> None:
        if not await cast(Awaitable[bool], self._redis.ping()):
            raise RuntimeError("Redis PING 未返回成功")

    async def _check_opensandbox(self) -> None:
        headers = {}
        if self._sandbox.api_key is not None:
            headers["OPEN-SANDBOX-API-KEY"] = self._sandbox.api_key.get_secret_value()
        response = await self._http_client.get(
            f"{self._sandbox.protocol}://{self._sandbox.domain}/health",
            headers=headers,
        )
        response.raise_for_status()

    async def _safe(self, check: Callable[[], Awaitable[None]]) -> bool:
        try:
            await asyncio.wait_for(check(), timeout=self._timeout_seconds)
        except (
            OSError,
            RedisError,
            RuntimeError,
            SQLAlchemyError,
            TimeoutError,
            httpx.HTTPError,
        ):
            return False
        return True

    async def check(self) -> dict[str, bool]:
        """返回不含异常和凭据的稳定组件状态"""

        mysql, redis, opensandbox = await asyncio.gather(
            self._safe(self._check_mysql),
            self._safe(self._check_redis),
            self._safe(self._check_opensandbox),
        )
        return {
            "mysql": mysql,
            "redis": redis,
            "opensandbox": opensandbox,
        }
