"""Real OpenSandbox rooted descriptor operations on disposable Docker resources."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from deepagents.backends.protocol import INVALID_PATH
from docker import DockerClient
from opensandbox.config import ConnectionConfig
from tests.support.docker_services import OpenSandboxTestService

from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxConfig
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
