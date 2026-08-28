"""Disposable Docker services shared by repository-wide integration tests.

The fixtures own every container they create and expose only loopback URLs with
random host ports. A missing Docker daemon skips Docker integration tests; once
the daemon responds, image pulls, startup, readiness, and cleanup failures remain
real test failures.
"""

from __future__ import annotations

import re
import secrets
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import (
    BaseHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import uuid4

import docker
import pytest
import pytest_asyncio
from docker import DockerClient
from docker.errors import DockerException, NotFound
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.config import get_docker_socket, testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import ExecWaitStrategy, HttpWaitStrategy
from testcontainers.core.waiting_utils import WaitStrategyTarget

_MYSQL_IMAGE = (
    "mysql:8.4@sha256:b3b90af2a6552ae30c266fdb7d5dd55f3afb72404bb78d37fe8a23eb857fd3fb"
)
_REDIS_STACK_IMAGE = (
    "redis/redis-stack-server:7.4.0-v8@sha256:"
    "798ab84d9f266936b034ab11c4d04a2b8e4b441884c5aa7d17ac951eefdf742a"
)
_OPENSANDBOX_SERVER_IMAGE = (
    "opensandbox/server:v0.2.2@sha256:"
    "8f8762af7565ed9c6f9dbcf009dd56727aa1fef8ce58a17f2b007b88cfe542bb"
)
_TEST_LABEL = "tinkerfin.test/run"
_SANDBOX_TEST_LABEL = "tinkerfin.test/sandbox-run"
_MYSQL_SANDBOX_DATABASE_PATTERN = re.compile(r"\Atinkerfin_sandbox_[a-f0-9]{32}\Z")
_DOCKER_FIXTURE_NAMES = frozenset(
    {
        "mysql_admin_url",
        "mysql_sandbox_url",
        "mysql_test_service",
        "opensandbox_test_service",
        "redis_checkpoint_url",
        "redis_test_service",
        "redis_url",
    }
)


class _MappedPortHttpWaitStrategy(HttpWaitStrategy):
    """Retry Docker's mapped-port publication before starting HTTP readiness.

    Docker Desktop can report a container as running a few milliseconds before its
    random host-port mapping becomes visible. Testcontainers 4.15 builds the HTTP URL
    once, so that narrow daemon race otherwise aborts a healthy container immediately.
    The probe re-resolves that mapping, bypasses host proxy settings for loopback, and
    keeps the inherited status-code and response checks within one startup timeout.
    """

    def __init__(
        self,
        port: int,
        path: str,
        *,
        mapping_timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(port, path)
        self._mapping_timeout_seconds = mapping_timeout_seconds

    def _build_url(self, container: WaitStrategyTarget) -> str:
        deadline = time.monotonic() + self._mapping_timeout_seconds
        while True:
            try:
                url = super()._build_url(container)
                if sys.platform == "darwin":
                    return url.replace("://localhost:", "://127.0.0.1:", 1)
                return url
            except ConnectionError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Docker did not publish the OpenSandbox host-port mapping "
                        f"within {self._mapping_timeout_seconds:g} seconds"
                    ) from error
                time.sleep(self._poll_interval)

    def wait_until_ready(self, container: WaitStrategyTarget) -> None:
        """Re-resolve a random host port on every bounded HTTP attempt."""

        started_at = time.monotonic()
        headers = self._setup_headers()
        ssl_context = self._setup_ssl_context()
        last_url = f"http://unresolved:{self._port}{self._path}"
        while True:
            if time.monotonic() - started_at > self._startup_timeout:
                self._raise_timeout_error(last_url)
            last_url = self._build_url(container)
            if self._try_http_request(last_url, headers, ssl_context):
                return
            time.sleep(self._poll_interval)

    def _try_http_request(
        self,
        url: str,
        headers: dict[str, str],
        ssl_context: Any,
    ) -> bool:
        """Probe loopback directly without macOS or host proxy configuration."""

        handlers: list[BaseHandler] = [ProxyHandler({})]
        if ssl_context is not None:
            handlers.append(HTTPSHandler(context=ssl_context))
        opener = build_opener(*handlers)
        request = Request(
            url,
            headers=headers,
            method=self._method,
            data=self._body.encode() if self._body else None,
        )
        try:
            with opener.open(request, timeout=1) as response:
                return self._check_response(response, url)
        except (URLError, HTTPError) as error:
            return self._handle_http_error(error)
        except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError, OSError):
            return False


def _with_opensandbox_port(container: DockerContainer) -> DockerContainer:
    """Expose OpenSandbox through a random host port safe for the current daemon."""

    container.with_exposed_ports(8090)
    if sys.platform == "darwin":
        # Docker Desktop can create a non-forwarding 0.0.0.0 random binding for this
        # Docker-socket-owning image. docker-py's explicit loopback + port 0 keeps the
        # port random while using the forwarding path verified by the host process.
        ports = cast(dict[str, object], container.ports)
        ports["8090"] = ("127.0.0.1", 0)
    return container


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Classify tests that consume a disposable Docker service fixture."""

    marker = pytest.mark.docker_integration
    for item in items:
        if isinstance(item, pytest.Function) and _DOCKER_FIXTURE_NAMES.intersection(
            item.fixturenames
        ):
            item.add_marker(marker)


@dataclass(frozen=True, slots=True)
class MySQLTestService:
    """One session-owned MySQL server with an internal random root password."""

    host: str
    port: int
    password: str

    def url(self, database: str) -> str:
        """Return one async SQLAlchemy URL for a database on this server."""

        return URL.create(
            drivername="mysql+asyncmy",
            username="root",
            password=self.password,
            host=self.host,
            port=self.port,
            database=database,
            query={"charset": "utf8mb4"},
        ).render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class RedisTestService:
    """One session-owned Redis Stack server without authentication."""

    host: str
    port: int

    def url(self, database: int) -> str:
        """Return one Redis URL for the selected logical database."""

        if database < 0:
            raise ValueError("Redis database must be non-negative")
        return f"redis://{self.host}:{self.port}/{database}"


@dataclass(frozen=True, slots=True)
class OpenSandboxTestService:
    """One session-owned OpenSandbox Server and its child ownership label."""

    domain: str
    api_key: str
    run_id: str

    @property
    def sandbox_metadata(self) -> dict[str, str]:
        """Return metadata that makes every child Sandbox exactly reclaimable."""

        return {_SANDBOX_TEST_LABEL: self.run_id}


@pytest.fixture(scope="session")
def docker_test_run_id() -> str:
    """Return the unique ownership label value for one Pytest process."""

    return uuid4().hex


@pytest.fixture(scope="session")
def docker_test_client(docker_test_run_id: str) -> Iterator[DockerClient]:
    """Yield a verified Docker client or skip tests when no daemon is available."""

    client: DockerClient | None = None
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as error:
        if client is not None:
            client.close()
        pytest.skip(f"Docker daemon is unavailable: {type(error).__name__}")
    assert client is not None
    # Docker Desktop on macOS exposes a VM-owned user socket that Ryuk cannot
    # bind mount. The exact-label finalizer remains active on every platform;
    # Linux CI additionally retains Testcontainers' out-of-process reaper.
    if sys.platform == "darwin":
        testcontainers_config.ryuk_disabled = True
    try:
        yield client
    finally:
        for label in (_SANDBOX_TEST_LABEL, _TEST_LABEL):
            owned = client.containers.list(
                all=True,
                filters={"label": f"{label}={docker_test_run_id}"},
            )
            for container in owned:
                try:
                    container.remove(force=True, v=True)
                except NotFound:
                    continue
        client.close()


@pytest.fixture(scope="session")
def redis_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> Iterator[RedisTestService]:
    """Start one Redis Stack container for ordinary and RediSearch tests."""

    del docker_test_client
    wait = (
        ExecWaitStrategy(
            [
                "sh",
                "-ec",
                "redis-cli ping | grep -q PONG && redis-cli FT._LIST >/dev/null",
            ]
        )
        .with_poll_interval(0.2)
        .with_startup_timeout(120)
    )
    container = (
        DockerContainer(_REDIS_STACK_IMAGE)
        .with_exposed_ports(6379)
        .with_kwargs(labels={_TEST_LABEL: docker_test_run_id})
        .waiting_for(wait)
    )
    with container:
        yield RedisTestService(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(6379),
        )


@pytest.fixture(scope="session")
def redis_url(redis_test_service: RedisTestService) -> str:
    """Return the ordinary Redis test URL isolated on logical DB 15."""

    return redis_test_service.url(15)


@pytest.fixture(scope="session")
def redis_checkpoint_url(redis_test_service: RedisTestService) -> str:
    """Return the RediSearch-compatible checkpoint URL on logical DB 0."""

    return redis_test_service.url(0)


@pytest.fixture(scope="session")
def mysql_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
) -> Iterator[MySQLTestService]:
    """Start one MySQL 8.4 container for repository database integrations."""

    del docker_test_client
    password = secrets.token_urlsafe(24)
    wait = (
        ExecWaitStrategy(
            [
                "mysqladmin",
                "ping",
                "-h",
                "127.0.0.1",
                "-uroot",
                f"-p{password}",
                "--silent",
            ]
        )
        .with_poll_interval(0.5)
        .with_startup_timeout(180)
    )
    container = (
        DockerContainer(_MYSQL_IMAGE)
        .with_env("MYSQL_ROOT_PASSWORD", password)
        .with_env("MYSQL_DATABASE", "tinkerfin_test_admin")
        .with_exposed_ports(3306)
        .with_kwargs(labels={_TEST_LABEL: docker_test_run_id})
        .waiting_for(wait)
    )
    with container:
        yield MySQLTestService(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(3306),
            password=password,
        )


@pytest.fixture(scope="session")
def mysql_admin_url(mysql_test_service: MySQLTestService) -> str:
    """Return the management URL used to create disposable test databases."""

    return mysql_test_service.url("tinkerfin_test_admin")


def _opensandbox_config() -> str:
    """Return the isolated Docker runtime configuration used by the E2E server."""

    return """\
[server]
host = "0.0.0.0"
port = 8090
max_sandbox_timeout_seconds = 1200

[log]
level = "INFO"
file_enabled = false

[runtime]
type = "docker"
execd_image = "opensandbox/execd:v1.0.22"

[storage]
allowed_host_paths = []
volume_default_size = "1Gi"

[store]
type = "sqlite"
path = "/tmp/opensandbox-test.db"

[docker]
network_mode = "bridge"
host_ip = "host.docker.internal"
port_range_min = 40000
port_range_max = 60000
drop_capabilities = [
    "AUDIT_WRITE",
    "MKNOD",
    "NET_ADMIN",
    "NET_RAW",
    "SYS_ADMIN",
    "SYS_MODULE",
    "SYS_PTRACE",
    "SYS_TIME",
    "SYS_TTY_CONFIG",
]
no_new_privileges = true
pids_limit = 4096

[ingress]
mode = "direct"

[egress]
image = "opensandbox/egress:v1.1.6"
mode = "dns"
"""


def _opensandbox_docker_socket() -> str:
    """Return a socket source mountable by the daemon hosting the test server."""

    # Docker Desktop translates the conventional VM socket even though the
    # client context exposes a user-owned proxy socket that cannot be mounted.
    if sys.platform == "darwin":
        return "/var/run/docker.sock"
    return get_docker_socket()


@pytest.fixture(scope="session")
def opensandbox_test_service(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[OpenSandboxTestService]:
    """Start an isolated OpenSandbox Server backed by the host Docker daemon."""

    del docker_test_client
    api_key = secrets.token_urlsafe(32)
    config_path: Path = tmp_path_factory.mktemp("opensandbox") / "config.toml"
    config_path.write_text(_opensandbox_config(), encoding="utf-8")
    wait = (
        _MappedPortHttpWaitStrategy(8090, "/health")
        .with_poll_interval(0.5)
        .with_startup_timeout(180)
    )
    container = (
        _with_opensandbox_port(DockerContainer(_OPENSANDBOX_SERVER_IMAGE))
        .with_env("OPENSANDBOX_SERVER_API_KEY", api_key)
        .with_volume_mapping(
            _opensandbox_docker_socket(),
            "/var/run/docker.sock",
            "rw",
        )
        .with_copy_into_container(config_path, "/etc/opensandbox/config.toml")
        .with_kwargs(
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={_TEST_LABEL: docker_test_run_id},
        )
        .waiting_for(wait)
    )
    with container:
        yield OpenSandboxTestService(
            domain=(
                f"{container.get_container_host_ip()}:"
                f"{container.get_exposed_port(8090)}"
            ),
            api_key=api_key,
            run_id=docker_test_run_id,
        )


@pytest_asyncio.fixture
async def mysql_sandbox_url(
    mysql_test_service: MySQLTestService,
) -> AsyncIterator[str]:
    """Create one database owned only by a Sandbox integration test."""

    database_name = f"tinkerfin_sandbox_{uuid4().hex}"
    if _MYSQL_SANDBOX_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("generated Sandbox database name is outside the owned scope")
    admin_engine = create_async_engine(mysql_test_service.url("tinkerfin_test_admin"))
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
        created = True
        yield mysql_test_service.url(database_name)
    finally:
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(f"DROP DATABASE IF EXISTS `{database_name}`")
                )
        await admin_engine.dispose()


__all__ = [
    "MySQLTestService",
    "OpenSandboxTestService",
    "RedisTestService",
]
