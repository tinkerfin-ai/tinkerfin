"""Pure contracts for repository Docker service descriptors."""

from collections.abc import Awaitable
from typing import Any, cast
from urllib.request import ProxyHandler

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import WaitStrategyTarget

from tests.support.docker_services import (
    MySQLTestService,
    RedisTestService,
    _MappedPortHttpWaitStrategy,
    _with_opensandbox_port,
)


class _DelayedMappedPort:
    """Expose a Docker host port only after two daemon-style lookup races."""

    def __init__(self, *, always_missing: bool = False) -> None:
        self.attempts = 0
        self.always_missing = always_missing

    def get_container_host_ip(self) -> str:
        return "127.0.0.1"

    def get_exposed_port(self, port: int) -> int:
        assert port == 8090
        self.attempts += 1
        if self.always_missing or self.attempts < 3:
            raise ConnectionError("mapping not published")
        return 49123


class _ChangingMappedPort(_DelayedMappedPort):
    """Return a new provisional random host port on each Docker lookup."""

    def get_exposed_port(self, port: int) -> int:
        assert port == 8090
        self.attempts += 1
        return 49120 + self.attempts


def test_opensandbox_wait_retries_delayed_docker_port_publication() -> None:
    target = _DelayedMappedPort()
    strategy = _MappedPortHttpWaitStrategy(
        8090,
        "/health",
        mapping_timeout_seconds=0.1,
    ).with_poll_interval(0)

    url = strategy._build_url(cast(WaitStrategyTarget, target))

    assert url == "http://127.0.0.1:49123/health"
    assert target.attempts == 3


def test_opensandbox_wait_bounds_missing_docker_port_publication() -> None:
    target = _DelayedMappedPort(always_missing=True)
    strategy = _MappedPortHttpWaitStrategy(
        8090,
        "/health",
        mapping_timeout_seconds=0,
    ).with_poll_interval(0)

    with pytest.raises(TimeoutError, match="host-port mapping"):
        strategy._build_url(cast(WaitStrategyTarget, target))


def test_opensandbox_http_wait_re_resolves_a_provisional_random_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _ChangingMappedPort()
    strategy = _MappedPortHttpWaitStrategy(
        8090,
        "/health",
        mapping_timeout_seconds=0.1,
    ).with_poll_interval(0)
    attempted_urls: list[str] = []

    def probe(url: str, headers: object, ssl_context: object) -> bool:
        del headers, ssl_context
        attempted_urls.append(url)
        return len(attempted_urls) == 2

    monkeypatch.setattr(strategy, "_try_http_request", probe)

    strategy.wait_until_ready(cast(WaitStrategyTarget, target))

    assert attempted_urls == [
        "http://127.0.0.1:49121/health",
        "http://127.0.0.1:49122/health",
    ]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ("127.0.0.1", 0)),
        ("linux", None),
    ],
)
def test_opensandbox_port_binding_matches_the_daemon_platform(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: object,
) -> None:
    monkeypatch.setattr("tests.support.docker_services.sys.platform", platform)
    container = _with_opensandbox_port(DockerContainer("fixture-image"))

    assert container.ports["8090"] == expected


def test_opensandbox_health_probe_ignores_host_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: list[object] = []

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class Opener:
        def open(self, request: object, *, timeout: int) -> Response:
            del request
            assert timeout == 1
            return Response()

    def opener(*handlers: object) -> Opener:
        captured_handlers.extend(handlers)
        return Opener()

    monkeypatch.setattr("tests.support.docker_services.build_opener", opener)
    strategy = _MappedPortHttpWaitStrategy(8090, "/health")

    assert strategy._try_http_request("http://127.0.0.1:50000/health", {}, None)
    proxy = next(
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    )
    assert cast(Any, proxy).proxies == {}


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
