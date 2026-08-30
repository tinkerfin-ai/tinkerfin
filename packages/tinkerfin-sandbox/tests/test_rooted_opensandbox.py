"""Real OpenSandbox rooted descriptor operations on disposable Docker resources."""

from __future__ import annotations

import asyncio
import secrets
import shlex
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from deepagents.backends.protocol import INVALID_PATH
from docker import DockerClient
from opensandbox.config import ConnectionConfig
from testcontainers.core.container import DockerContainer
from tests.support.docker_services import (
    OpenSandboxTestService,
    _MappedPortHttpWaitStrategy,
    _opensandbox_config,
    _opensandbox_docker_socket,
    _with_opensandbox_port,
)

from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
    SQLAlchemyOpenSandboxState,
)
from tinkerfin_sandbox.backends import _rooted_protocol
from tinkerfin_sandbox.backends.sdk import OpenSandboxBackend


@dataclass(frozen=True, slots=True)
class RaceCase:
    """One deterministic target-component replacement scenario."""

    name: str
    virtual_path: str
    outside_target: str
    setup_command: str
    swap_command: str


def _install_transfer_barrier(*, reached: str, release: str) -> str:
    original = _rooted_protocol._ROOTED_HELPER_SCRIPT
    function_start = original.index("def transfer_file(")
    function_end = original.find("\ndef ", function_start + 1)
    function_source = original[function_start:function_end]
    statement = "        canonical = canonical_parts(root, virtual_parts)"
    barrier = (
        statement
        + "\n"
        + f"        open({reached!r}, 'x').close()\n"
        + f"        while not os.path.exists({release!r}):\n"
        + "            time.sleep(0.01)"
    )
    if function_source.count(statement) != 1:
        raise RuntimeError("transfer helper barrier location is ambiguous")
    _rooted_protocol._ROOTED_HELPER_SCRIPT = (
        original[:function_start]
        + function_source.replace(statement, barrier)
        + original[function_end:]
    )
    return original


async def _wait_for_path(
    backend: OpenSandboxBackend,
    path: str,
    *,
    timeout: float = 10.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    quoted = shlex.quote(path)
    while asyncio.get_running_loop().time() < deadline:
        if (await backend.aexecute(f"test -e {quoted}")).exit_code == 0:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"remote barrier was not reached: {path}")


async def _assert_outside_sentinel(
    backend: OpenSandboxBackend,
    path: str,
) -> None:
    response = (await backend.adownload_files([path]))[0]
    if response.error is not None or response.content != b"outside sentinel":
        raise AssertionError(f"outside sentinel changed: {path}, {response.error}")


async def _run_race(
    backend: OpenSandboxBackend,
    case: RaceCase,
    *,
    token: str,
) -> None:
    reached = f"/tmp/{token}-{case.name}.reached"
    release = f"/tmp/{token}-{case.name}.release"
    await backend.aexecute(case.setup_command)
    original = _install_transfer_barrier(reached=reached, release=release)
    transfer = None
    try:
        transfer = asyncio.create_task(
            backend._aupload_rooted_file(
                root="/workspace",
                path=case.virtual_path,
                content=b"must not reach outside",
            )
        )
        await _wait_for_path(backend, reached)
        swap = await backend.aexecute(case.swap_command)
        if swap.exit_code != 0:
            raise RuntimeError(f"race swap failed: {case.name}: {swap.output}")
        await backend.aexecute(f"touch {shlex.quote(release)}")
        response = await transfer
        if response.error != INVALID_PATH:
            raise AssertionError(
                f"race did not reject target: {case.name}: {response.error}"
            )
        await _assert_outside_sentinel(backend, case.outside_target)
    finally:
        _rooted_protocol._ROOTED_HELPER_SCRIPT = original
        try:
            await backend.aexecute(f"touch {shlex.quote(release)}")
        finally:
            if transfer is not None and not transfer.done():
                await asyncio.gather(transfer, return_exceptions=True)


def _race_cases(token: str) -> tuple[RaceCase, ...]:
    workspace_base = f"/workspace/{token}"
    outside_base = f"/tmp/{token}-outside"
    return (
        RaceCase(
            name="leaf",
            virtual_path=f"/{token}/leaf/target.bin",
            outside_target=f"{outside_base}/leaf.bin",
            setup_command=(
                f"mkdir -p {workspace_base}/leaf {outside_base}; "
                f"printf 'inside' > {workspace_base}/leaf/target.bin; "
                f"printf 'outside sentinel' > {outside_base}/leaf.bin"
            ),
            swap_command=(
                f"rm -f {workspace_base}/leaf/target.bin; "
                f"ln -s {outside_base}/leaf.bin "
                f"{workspace_base}/leaf/target.bin"
            ),
        ),
        RaceCase(
            name="parent",
            virtual_path=f"/{token}/parent/target.bin",
            outside_target=f"{outside_base}/parent/target.bin",
            setup_command=(
                f"mkdir -p {workspace_base}/parent {outside_base}/parent; "
                f"printf 'inside' > {workspace_base}/parent/target.bin; "
                f"printf 'outside sentinel' > {outside_base}/parent/target.bin"
            ),
            swap_command=(
                f"mv {workspace_base}/parent {workspace_base}/parent-detached; "
                f"ln -s {outside_base}/parent {workspace_base}/parent"
            ),
        ),
        RaceCase(
            name="multilevel",
            virtual_path=f"/{token}/level1/level2/target.bin",
            outside_target=f"{outside_base}/multilevel/target.bin",
            setup_command=(
                f"mkdir -p {workspace_base}/level1/level2 "
                f"{outside_base}/multilevel; "
                f"printf 'inside' > "
                f"{workspace_base}/level1/level2/target.bin; "
                f"printf 'outside sentinel' > "
                f"{outside_base}/multilevel/target.bin"
            ),
            swap_command=(
                f"mv {workspace_base}/level1/level2 "
                f"{workspace_base}/level1/level2-detached; "
                f"ln -s {outside_base}/multilevel "
                f"{workspace_base}/level1/level2"
            ),
        ),
    )


async def _wait_for_child_cleanup(
    docker_client: DockerClient,
    metadata: dict[str, str],
    *,
    timeout: float = 10.0,
) -> None:
    label, value = next(iter(metadata.items()))
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        owned = docker_client.containers.list(
            all=True,
            filters={"label": f"{label}={value}"},
        )
        if not owned:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("OpenSandbox child container was not removed")


async def _owned_sandbox_ids(
    docker_client: DockerClient,
    *,
    label: str,
    value: str,
) -> tuple[str, ...]:
    """Return current remote IDs without blocking the async test loop."""

    containers = await asyncio.to_thread(
        docker_client.containers.list,
        all=True,
        filters={"label": f"{label}={value}"},
    )
    return tuple(str(container.labels["opensandbox.io/id"]) for container in containers)


async def _wait_for_owned_sandbox_count(
    docker_client: DockerClient,
    *,
    label: str,
    value: str,
    count: int,
    timeout: float = 15.0,
) -> tuple[str, ...]:
    """Wait for one exact number of test-owned remote Sandboxes."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        sandbox_ids = await _owned_sandbox_ids(
            docker_client,
            label=label,
            value=value,
        )
        if len(sandbox_ids) == count:
            return sandbox_ids
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} owned Sandboxes")


def _recreated_opensandbox_server(
    *,
    api_key: str,
    config_path: Path,
    metadata_dir: Path,
    run_id: str,
) -> DockerContainer:
    """Build one disposable Server sharing only Docker and runtime metadata."""

    wait = (
        _MappedPortHttpWaitStrategy(8090, "/health")
        .with_poll_interval(0.5)
        .with_startup_timeout(180)
    )
    return (
        _with_opensandbox_port(
            DockerContainer(
                "opensandbox/server:v0.2.2@sha256:"
                "8f8762af7565ed9c6f9dbcf009dd56727aa1fef8ce58a17f2b007b88cfe542bb"
            )
        )
        .with_env("OPENSANDBOX_SERVER_API_KEY", api_key)
        .with_volume_mapping(
            _opensandbox_docker_socket(),
            "/var/run/docker.sock",
            "rw",
        )
        .with_volume_mapping(
            str(metadata_dir),
            "/root/.opensandbox/metadata",
            "rw",
        )
        .with_copy_into_container(config_path, "/etc/opensandbox/config.toml")
        .with_kwargs(
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={"tinkerfin.test/run": run_id},
        )
        .waiting_for(wait)
    )


@pytest.mark.opensandbox_e2e
async def test_real_rooted_descriptor_transfers_reject_symlink_races(
    opensandbox_test_service: OpenSandboxTestService,
    docker_test_client: DockerClient,
) -> None:
    """Exercise one real Sandbox and prove every owned container is destroyed."""

    token = f"tinkerfin-rooted-{uuid4().hex}"
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain=opensandbox_test_service.domain,
            api_key=opensandbox_test_service.api_key,
            request_timeout=timedelta(minutes=3),
            use_server_proxy=True,
        ),
        config=OpenSandboxConfig(
            workspace_root="/workspace",
            warm_pool_size=0,
            ttl=timedelta(minutes=20),
            command_timeout=180,
        ),
    )
    backend: OpenSandboxBackend | None = None
    completed: list[str] = []
    try:
        backend = await client.create(
            metadata={
                "purpose": "rooted-integration",
                **opensandbox_test_service.sandbox_metadata,
            }
        )
        content = bytes(range(256)) * (64 * 1024)
        large_path = f"/{token}/large.bin"
        uploaded = await backend._aupload_rooted_file(
            root="/workspace",
            path=large_path,
            content=content,
        )
        assert uploaded.error is None
        downloaded = await backend._adownload_rooted_file(
            root="/workspace",
            path=large_path,
        )
        assert downloaded.error is None
        assert downloaded.content == content
        completed.append("descriptor_transfer_16mib")

        for case in _race_cases(token):
            await _run_race(backend, case, token=token)
            completed.append(f"race_{case.name}")
    finally:
        if backend is not None:
            try:
                await backend.akill()
            finally:
                await backend.aclose()
        await client.aclose()

    assert completed == [
        "descriptor_transfer_16mib",
        "race_leaf",
        "race_parent",
        "race_multilevel",
    ]
    await _wait_for_child_cleanup(
        docker_test_client,
        opensandbox_test_service.sandbox_metadata,
    )


@pytest.mark.opensandbox_e2e
async def test_real_manager_renews_warm_ttl_and_replaces_it_after_restart(
    opensandbox_test_service: OpenSandboxTestService,
    docker_test_client: DockerClient,
    tmp_path: Path,
) -> None:
    """Prove running renewal and durable stale-slot recovery against Docker."""

    purpose = f"warm-lifecycle-{uuid4().hex}"
    purpose_label = "purpose"
    config = OpenSandboxConfig(
        workspace_root="/workspace",
        warm_pool_size=1,
        ttl=timedelta(seconds=60),
        metadata={
            purpose_label: purpose,
            **opensandbox_test_service.sandbox_metadata,
        },
    )
    connection = ConnectionConfig(
        domain=opensandbox_test_service.domain,
        api_key=opensandbox_test_service.api_key,
        request_timeout=timedelta(minutes=2),
        use_server_proxy=True,
    )
    state_url = f"sqlite+aiosqlite:///{tmp_path / 'warm-lifecycle.db'}"
    first_client = OpenSandboxClient(connection_config=connection, config=config)
    first = OpenSandboxManager[str](
        client=first_client,
        key_resolver=lambda value: value,
        state=SQLAlchemyOpenSandboxState(
            url=state_url,
            namespace=purpose,
            lease_ttl=1.0,
            poll_interval=0.02,
        ),
        warm_pool_size=1,
        fail_on_startup_warmup_error=True,
    )
    second: OpenSandboxManager[str] | None = None
    cleanup_client: OpenSandboxClient | None = None
    try:
        await first.start()
        (original_id,) = await _wait_for_owned_sandbox_count(
            docker_test_client,
            label=purpose_label,
            value=purpose,
            count=1,
        )
        initial_info = await first_client.inspect(original_id)
        assert initial_info.expires_at is not None
        await asyncio.sleep(22)
        await first.check_ready()
        renewed_info = await first_client.inspect(original_id)
        assert renewed_info.expires_at is not None
        assert renewed_info.expires_at > initial_info.expires_at
        assert await _owned_sandbox_ids(
            docker_test_client,
            label=purpose_label,
            value=purpose,
        ) == (original_id,)

        await first.aclose()
        expiry_client = OpenSandboxClient(connection_config=connection, config=config)
        expiring_backend = await expiry_client.connect(original_id)
        try:
            await expiring_backend.arenew(timedelta(seconds=3))
        finally:
            await expiring_backend.aclose()
            await expiry_client.aclose()
        await _wait_for_owned_sandbox_count(
            docker_test_client,
            label=purpose_label,
            value=purpose,
            count=0,
            timeout=10.0,
        )

        second_client = OpenSandboxClient(connection_config=connection, config=config)
        second = OpenSandboxManager[str](
            client=second_client,
            key_resolver=lambda value: value,
            state=SQLAlchemyOpenSandboxState(
                url=state_url,
                namespace=purpose,
                lease_ttl=1.0,
                poll_interval=0.02,
            ),
            warm_pool_size=1,
            fail_on_startup_warmup_error=True,
        )
        await second.start()
        await second.check_ready()
        (replacement_id,) = await _wait_for_owned_sandbox_count(
            docker_test_client,
            label=purpose_label,
            value=purpose,
            count=1,
        )
        assert replacement_id != original_id
        owner = await second.get("owner")
        assert owner.id == replacement_id
        assert (await owner.aexecute("printf ready")).exit_code == 0
    finally:
        if second is not None:
            await second.aclose()
        await first.aclose()
        cleanup_client = OpenSandboxClient(connection_config=connection, config=config)
        for sandbox_id in await _owned_sandbox_ids(
            docker_test_client,
            label=purpose_label,
            value=purpose,
        ):
            await cleanup_client.destroy(sandbox_id)
        await cleanup_client.aclose()
        await _wait_for_owned_sandbox_count(
            docker_test_client,
            label=purpose_label,
            value=purpose,
            count=0,
        )


@pytest.mark.docker_integration
@pytest.mark.opensandbox_e2e
async def test_recreated_server_restores_persisted_expiration_override(
    docker_test_client: DockerClient,
    docker_test_run_id: str,
    tmp_path: Path,
) -> None:
    """A Server replacement must honor the latest Docker expiration metadata."""

    api_key = secrets.token_urlsafe(32)
    config_path = tmp_path / "config.toml"
    await asyncio.to_thread(
        config_path.write_text,
        _opensandbox_config(),
        encoding="utf-8",
    )
    metadata_dir = tmp_path / "metadata"
    await asyncio.to_thread(metadata_dir.mkdir)
    host_metadata_dir = Path.home() / ".opensandbox" / "metadata"
    if await asyncio.to_thread(host_metadata_dir.is_dir):
        await asyncio.to_thread(
            shutil.copytree,
            host_metadata_dir,
            metadata_dir,
            dirs_exist_ok=True,
        )
    purpose = f"server-restart-{uuid4().hex}"
    purpose_label = "purpose"
    sandbox_id: str | None = None
    try:
        first_server = _recreated_opensandbox_server(
            api_key=api_key,
            config_path=config_path,
            metadata_dir=metadata_dir,
            run_id=docker_test_run_id,
        )
        await asyncio.to_thread(first_server.start)
        try:
            first_domain = await asyncio.to_thread(
                lambda: (
                    f"{first_server.get_container_host_ip()}:"
                    f"{first_server.get_exposed_port(8090)}"
                )
            )
            connection = ConnectionConfig(
                domain=first_domain,
                api_key=api_key,
                request_timeout=timedelta(minutes=2),
                use_server_proxy=True,
            )
            client = OpenSandboxClient(
                connection_config=connection,
                config=OpenSandboxConfig(
                    workspace_root="/workspace",
                    warm_pool_size=0,
                    ttl=timedelta(seconds=60),
                    metadata={
                        purpose_label: purpose,
                        "tinkerfin.test/sandbox-run": docker_test_run_id,
                    },
                ),
            )
            backend = await client.create()
            sandbox_id = backend.id
            try:
                await backend.arenew(timedelta(seconds=12))
            finally:
                await backend.aclose()
                await client.aclose()
            expiration_file = metadata_dir / "_expiration" / f"{sandbox_id}.json"
            assert await asyncio.to_thread(expiration_file.is_file)
        finally:
            await asyncio.to_thread(first_server.stop)

        second_server = _recreated_opensandbox_server(
            api_key=api_key,
            config_path=config_path,
            metadata_dir=metadata_dir,
            run_id=docker_test_run_id,
        )
        await asyncio.to_thread(second_server.start)
        try:
            await _wait_for_owned_sandbox_count(
                docker_test_client,
                label=purpose_label,
                value=purpose,
                count=0,
                timeout=20.0,
            )
        finally:
            await asyncio.to_thread(second_server.stop)
    finally:
        remaining = await asyncio.to_thread(
            docker_test_client.containers.list,
            all=True,
            filters={"label": f"{purpose_label}={purpose}"},
        )
        for container in remaining:
            await asyncio.to_thread(container.remove, force=True)
