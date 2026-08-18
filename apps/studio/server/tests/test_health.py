"""外部依赖 readiness 检查测试。"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

import httpx
from redis.asyncio import Redis

from tinkerfin_studio.config.settings import SandboxSettings
from tinkerfin_studio.health import ReadinessService
from tinkerfin_studio.infrastructure.database import Database


class _Connection:
    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, statement: object) -> None:
        del statement


class _Engine:
    def connect(self) -> _Connection:
        return _Connection()


class _Database:
    engine = _Engine()


class _Redis:
    async def ping(self) -> bool:
        return True


async def test_readiness_checks_all_dependencies_concurrently() -> None:
    """三个依赖全部可用时 readiness 必须逐项返回成功。"""

    async def sandbox(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://opensandbox:8090/health"
        return httpx.Response(200, json={"status": "healthy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http_client:
        service = ReadinessService(
            database=cast(Database, _Database()),
            redis=cast(Redis, _Redis()),
            sandbox=SandboxSettings(
                domain="opensandbox:8090",
                protocol="http",
                api_key=None,
                warm_pool_size=1,
                workspace_root="/workspace",
                state_namespace="studio",
            ),
            http_client=http_client,
            timeout_seconds=1,
        )

        assert await service.check() == {
            "mysql": True,
            "redis": True,
            "opensandbox": True,
        }


async def test_readiness_converts_dependency_errors_to_false() -> None:
    """依赖异常不得逃逸或暴露到健康响应。"""

    class BrokenRedis:
        def ping(self) -> Awaitable[bool]:
            raise RuntimeError("credential-bearing failure")

    async def sandbox(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http_client:
        service = ReadinessService(
            database=cast(Database, _Database()),
            redis=cast(Redis, BrokenRedis()),
            sandbox=SandboxSettings(
                domain="opensandbox:8090",
                protocol="http",
                api_key=None,
                warm_pool_size=1,
                workspace_root="/workspace",
                state_namespace="studio",
            ),
            http_client=http_client,
            timeout_seconds=1,
        )

        result = await service.check()

    assert result == {"mysql": True, "redis": False, "opensandbox": False}
