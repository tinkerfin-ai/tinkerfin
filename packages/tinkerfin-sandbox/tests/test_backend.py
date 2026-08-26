from __future__ import annotations

import asyncio
import base64
import json
import shlex
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest
from deepagents.backends.protocol import INVALID_PATH, ExecuteResponse
from opensandbox import Sandbox
from opensandbox import SandboxManager as OpenSandboxSDKManager
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import (
    PVC,
    PagedSandboxInfos,
    PaginationInfo,
    SandboxFilter,
    SandboxInfo,
    SandboxStatus,
    Volume,
)
from pydantic import ValidationError

from tinkerfin_sandbox import (
    OpenSandboxBackend,
    OpenSandboxBackendUnavailableError,
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxRuntimeInfo,
    UnexpectedOpenSandboxBackendError,
)


class _FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.result = SimpleNamespace(
            exit_code=7,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text="first"), SimpleNamespace(text="second")],
                stderr=[SimpleNamespace(text="warning")],
            ),
        )
        self.error: Exception | None = None

    async def run(self, command: str, *, opts: object | None = None) -> object:
        self.calls.append((command, opts))
        if self.error is not None:
            raise self.error
        return self.result


class _FakeFiles:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}
        self.read_calls: list[str] = []
        self.read_errors: dict[str, Exception] = {}
        self.write_errors: dict[str, Exception] = {}
        self.created_directories: list[str] = []
        self.created_directory_modes: list[int] = []
        self.write_calls: list[tuple[str, bytes, int]] = []
        self.create_directories_error: Exception | None = None

    async def read_bytes(self, path: str) -> bytes:
        self.read_calls.append(path)
        if path in self.read_errors:
            raise self.read_errors[path]
        return self.contents[path]

    async def create_directories(self, entries: list[Any]) -> None:
        if self.create_directories_error is not None:
            raise self.create_directories_error
        self.created_directories.extend(entry.path for entry in entries)
        self.created_directory_modes.extend(entry.mode for entry in entries)

    async def write_file(
        self,
        path: str,
        data: bytes,
        *,
        mode: int = 755,
    ) -> None:
        self.write_calls.append((path, data, mode))
        if path in self.write_errors:
            raise self.write_errors[path]
        self.contents[path] = data


class _FakeSandbox:
    def __init__(self, sandbox_id: str = "sandbox-1") -> None:
        self.id = sandbox_id
        self.commands = _FakeCommands()
        self.files = _FakeFiles()
        self.closed = False
        self.killed = False
        self.close_error: Exception | None = None
        self.kill_error: Exception | None = None
        self.renewed_for: timedelta | None = None
        self.info = SimpleNamespace(
            id=sandbox_id,
            status=SimpleNamespace(
                state="RUNNING",
                reason=None,
                message=None,
                last_transition_at=datetime(2026, 8, 8, tzinfo=UTC),
            ),
            entrypoint=["/entrypoint.sh"],
            expires_at=datetime(2026, 8, 9, tzinfo=UTC),
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
            image=SimpleNamespace(image="registry.example/sandbox:1"),
            platform=SimpleNamespace(os="linux", arch="amd64"),
            metadata={"purpose": "test"},
        )

    async def renew(self, timeout: timedelta) -> None:
        self.renewed_for = timeout

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def kill(self) -> None:
        self.killed = True
        if self.kill_error is not None:
            raise self.kill_error

    async def get_info(self) -> object:
        return self.info


class _FakeDescriptorCommands(_FakeCommands):
    def __init__(self) -> None:
        super().__init__()
        self.execution_id = "descriptor-execution-1"
        self.interrupt_calls: list[str] = []
        self.log_calls: list[tuple[str, int | None]] = []
        self.status_calls: list[str] = []
        self._handshake = ""
        self.handshake_override: str | None = None
        self.helper_error: tuple[str, str] | None = None
        self.interrupt_error: Exception | None = None
        self.status_error: Exception | None = None
        self.running = True

    async def run(self, command: str, *, opts: object | None = None) -> object:
        self.calls.append((command, opts))
        encoded_request = shlex.split(command)[-1]
        request = json.loads(base64.b64decode(encoded_request).decode("utf-8"))
        if self.helper_error is not None:
            code, message = self.helper_error
            self._handshake = json.dumps(
                {
                    "version": request["version"],
                    "request_id": request["request_id"],
                    "operation": request["operation"],
                    "status": "error",
                    "error": {"code": code, "message": message},
                    "result": None,
                }
            )
        elif self.handshake_override is not None:
            self._handshake = self.handshake_override
        else:
            self._handshake = json.dumps(
                {
                    "version": "tinkerfin.rooted.transfer.v1",
                    "token": request["arguments"]["token"],
                    "mode": request["arguments"]["mode"],
                    "pid": 4321,
                    "fd": 9,
                }
            )
        self.running = self.helper_error is None and self._handshake != ""
        return SimpleNamespace(
            id=self.execution_id,
            exit_code=None,
            logs=SimpleNamespace(stdout=[], stderr=[]),
        )

    async def get_background_command_logs(
        self,
        execution_id: str,
        cursor: int | None = None,
    ) -> object:
        self.log_calls.append((execution_id, cursor))
        return SimpleNamespace(content=self._handshake + "\n", cursor=1)

    async def interrupt(self, execution_id: str) -> None:
        self.interrupt_calls.append(execution_id)
        if self.interrupt_error is not None:
            raise self.interrupt_error
        self.running = False

    async def get_command_status(self, execution_id: str) -> object:
        self.status_calls.append(execution_id)
        if self.status_error is not None:
            raise self.status_error
        return SimpleNamespace(
            id=execution_id,
            running=self.running,
            exit_code=None if self.running else 143,
            error=None,
        )


class _FakeOffloadCommands(_FakeCommands):
    def __init__(self) -> None:
        super().__init__()
        self.malformed = False
        self.request: dict[str, Any] | None = None

    async def run(self, command: str, *, opts: object | None = None) -> object:
        self.calls.append((command, opts))
        encoded_request = shlex.split(command)[-1]
        request = cast(
            dict[str, Any],
            json.loads(base64.b64decode(encoded_request).decode("utf-8")),
        )
        self.request = request
        output = (
            "malformed offload response"
            if self.malformed
            else json.dumps(
                {
                    "version": request["version"],
                    "request_id": request["request_id"],
                    "operation": "offload",
                    "status": "ok",
                    "error": None,
                    "result": {
                        "offloaded": True,
                        "output": "preview",
                        "exit_code": 7,
                        "truncated": True,
                    },
                }
            )
        )
        return SimpleNamespace(
            id=None,
            exit_code=0,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=output)],
                stderr=[],
            ),
        )


def _sandbox_info(
    sandbox_id: str,
    *,
    metadata: dict[str, str],
) -> SandboxInfo:
    return SandboxInfo(
        id=sandbox_id,
        status=SandboxStatus(state="RUNNING"),
        entrypoint=["/entrypoint.sh"],
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        metadata=metadata,
    )


class _FakeOpenSandboxSDKManager:
    def __init__(self, sandbox_infos: list[SandboxInfo]) -> None:
        self.filters: list[SandboxFilter] = []
        self.killed_ids: list[str] = []
        self.closed = False
        self.close_error: Exception | None = None
        self._sandbox_infos = sandbox_infos

    async def list_sandbox_infos(self, filter: SandboxFilter) -> PagedSandboxInfos:
        self.filters.append(filter)
        page_number = filter.page or 1
        page_size = filter.page_size or 20
        total_items = len(self._sandbox_infos)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        start = (page_number - 1) * page_size
        return PagedSandboxInfos(
            sandbox_infos=self._sandbox_infos[start : start + page_size],
            pagination=PaginationInfo(
                page=page_number,
                page_size=page_size,
                total_items=total_items,
                total_pages=total_pages,
                has_next_page=page_number < total_pages,
            ),
        )

    async def kill_sandbox(self, sandbox_id: str) -> None:
        self.killed_ids.append(sandbox_id)

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class OpenSandboxConfigTests(unittest.TestCase):
    def test_rejects_blank_or_negative_boundary_values(self) -> None:
        invalid_values = (
            {"image": " "},
            {"command_timeout": -1},
            {"warm_pool_size": -1},
            {"ttl": timedelta(seconds=-1)},
            {"lifecycle_request_timeout": timedelta(0)},
            {"ready_timeout": timedelta(seconds=-1)},
            {"connect_timeout": timedelta(seconds=-1)},
            {"health_command": " "},
        )

        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                OpenSandboxConfig.model_validate(values)


class OpenSandboxBackendTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _set_command_result(
        sandbox: _FakeSandbox,
        output: str,
        *,
        exit_code: int = 0,
    ) -> None:
        sandbox.commands.result = SimpleNamespace(
            exit_code=exit_code,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=output)] if output else [],
                stderr=[],
            ),
        )

    async def test_adelete_rejects_unsafe_root_paths_before_remote_io(self) -> None:
        for file_path in ("/", "/.", "/tmp/..", "relative", "/tmp/\x00file"):
            with self.subTest(file_path=file_path):
                sandbox = _FakeSandbox()
                backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

                result = await backend.adelete(file_path)

                self.assertEqual(result.error, INVALID_PATH)
                self.assertEqual(sandbox.commands.calls, [])

    async def test_execute_passes_default_timeout_and_environment_to_sdk(self) -> None:
        sandbox = _FakeSandbox()
        backend = OpenSandboxBackend(
            sandbox=cast(Sandbox, sandbox),
            default_timeout=45,
            command_env={"PATH": "/opt/bin:/usr/bin", "LANG": "C.UTF-8"},
        )

        response = await backend.aexecute("printf hello")

        command, raw_opts = sandbox.commands.calls[0]
        opts = cast(RunCommandOpts, raw_opts)
        self.assertEqual(command, "printf hello")
        self.assertEqual(opts.timeout, timedelta(seconds=45))
        self.assertEqual(
            opts.envs,
            {"PATH": "/opt/bin:/usr/bin", "LANG": "C.UTF-8"},
        )
        self.assertEqual(response.exit_code, 7)
        self.assertIn("first", response.output)
        self.assertIn("second", response.output)
        self.assertIn("warning", response.output)
        self.assertFalse(response.truncated)

    async def test_execute_maps_explicit_zero_timeout_to_sdk_no_timeout(self) -> None:
        sandbox = _FakeSandbox()
        backend = OpenSandboxBackend(
            sandbox=cast(Sandbox, sandbox),
            default_timeout=45,
        )

        await backend.aexecute("true", timeout=0)

        opts = cast(RunCommandOpts, sandbox.commands.calls[0][1])
        self.assertIsNone(opts.timeout)

    async def test_sync_remote_methods_reject_instead_of_bridging_async_sdk(
        self,
    ) -> None:
        backend = OpenSandboxBackend(sandbox=cast(Sandbox, _FakeSandbox()))

        for operation in (
            lambda: backend.execute("true"),
            lambda: backend.download_files(["/data.bin"]),
            lambda: backend.upload_files([("/data.bin", b"data")]),
            backend.close,
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(
                    RuntimeError,
                    "asynchronous remote I/O only",
                ),
            ):
                operation()

    async def test_download_files_preserves_binary_data_and_partial_success(
        self,
    ) -> None:
        sandbox = _FakeSandbox()
        binary = b"\x00\xff\x80not-utf8"
        sandbox.files.contents["/data/blob.bin"] = binary
        sandbox.files.read_errors["/data/missing.bin"] = FileNotFoundError("missing")
        backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

        responses = await backend.adownload_files(
            ["relative.bin", "/data/blob.bin", "/data/missing.bin"]
        )

        self.assertEqual(
            [response.path for response in responses],
            [
                "relative.bin",
                "/data/blob.bin",
                "/data/missing.bin",
            ],
        )
        self.assertEqual(responses[0].error, "invalid_path")
        self.assertIsNone(responses[0].content)
        self.assertEqual(responses[1].content, binary)
        self.assertIsNone(responses[1].error)
        self.assertIsNone(responses[2].content)
        self.assertIsNotNone(responses[2].error)

    async def test_upload_files_creates_parents_and_isolates_per_file_errors(
        self,
    ) -> None:
        sandbox = _FakeSandbox()
        sandbox.files.write_errors["/bad/fails.bin"] = PermissionError("read-only")
        backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))
        binary = b"\x00\xff\x80not-utf8"

        responses = await backend.aupload_files(
            [
                ("relative.bin", b"ignored"),
                ("/nested/data/blob.bin", binary),
                ("/bad/fails.bin", b"failure"),
            ]
        )

        self.assertEqual(
            [response.path for response in responses],
            [
                "relative.bin",
                "/nested/data/blob.bin",
                "/bad/fails.bin",
            ],
        )
        self.assertEqual(responses[0].error, "invalid_path")
        self.assertIsNone(responses[1].error)
        self.assertIsNotNone(responses[2].error)
        self.assertEqual(sandbox.files.contents["/nested/data/blob.bin"], binary)
        self.assertNotIn(("relative.bin", b"ignored", 644), sandbox.files.write_calls)
        self.assertIn("/nested/data", sandbox.files.created_directories)
        self.assertIn("/bad", sandbox.files.created_directories)
        self.assertEqual(sandbox.files.created_directory_modes, [755, 755])
        self.assertIn(("/nested/data/blob.bin", binary, 644), sandbox.files.write_calls)

    async def test_inherited_base_sandbox_file_operations_use_sdk_adapter(
        self,
    ) -> None:
        sandbox = _FakeSandbox()
        backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

        self._set_command_result(
            sandbox,
            json.dumps(
                {
                    "encoding": "utf-8",
                    "content": "first\nsecond",
                    "total_lines": 2,
                    "start_line": 1,
                    "end_line": 2,
                    "next_offset": None,
                }
            ),
        )
        read_result = await backend.aread("/workspace/a.py", 0, 2)
        assert read_result.file_data is not None
        self.assertEqual(read_result.file_data["content"], "first\nsecond")

        self._set_command_result(sandbox, "")
        write_result = await backend.awrite("/workspace/a.py", "print('ok')")
        self.assertEqual(write_result.path, "/workspace/a.py")
        self.assertEqual(sandbox.files.contents["/workspace/a.py"], b"print('ok')")

        self._set_command_result(sandbox, json.dumps({"count": 2}))
        edit_result = await backend.aedit(
            "/workspace/a.py",
            "old",
            "new",
            replace_all=True,
        )
        self.assertEqual(edit_result.path, "/workspace/a.py")
        self.assertEqual(edit_result.occurrences, 2)

        self._set_command_result(sandbox, "")
        delete_result = await backend.adelete("/workspace/a.py")
        self.assertEqual(delete_result.path, "/workspace/a.py")

        self._set_command_result(
            sandbox,
            json.dumps({"path": "/workspace/a.py", "is_dir": False}),
        )
        ls_result = await backend.als("/workspace")
        self.assertEqual(
            ls_result.entries,
            [{"path": "/workspace/a.py", "is_dir": False}],
        )

        self._set_command_result(
            sandbox,
            json.dumps({"path": "a.py", "is_dir": False}),
        )
        glob_result = await backend.aglob("**/*.py", "/workspace")
        self.assertEqual(
            glob_result.matches,
            [{"path": "a.py", "is_dir": False}],
        )

        self._set_command_result(sandbox, "/workspace/a.py\x002:needle here")
        grep_result = await backend.agrep("needle", "/workspace", "*.py")
        self.assertEqual(
            grep_result.matches,
            [{"path": "/workspace/a.py", "line": 2, "text": "needle here"}],
        )


@pytest.mark.asyncio
async def test_rooted_descriptor_upload_uses_proc_fd_and_settles_helper() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    response = await backend._aupload_rooted_file(
        root="/workspace",
        path="/target.bin",
        content=b"descriptor content",
    )

    assert response.path == "/target.bin"
    assert response.error is None
    assert sandbox.files.write_calls == [
        ("/proc/4321/fd/9", b"descriptor content", 644)
    ]
    assert "/workspace/target.bin" not in sandbox.files.contents
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]
    _, raw_opts = commands.calls[0]
    opts = cast(RunCommandOpts, raw_opts)
    assert opts.background is True
    assert opts.working_directory == "/"
    assert opts.envs == {
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PYTHONSAFEPATH": "1",
    }


@pytest.mark.asyncio
async def test_rooted_descriptor_download_uses_proc_fd_and_settles_helper() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    sandbox.commands = commands
    sandbox.files.contents["/proc/4321/fd/9"] = b"downloaded content"
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    response = await backend._adownload_rooted_file(
        root="/workspace",
        path="/target.bin",
    )

    assert response.path == "/target.bin"
    assert response.content == b"downloaded content"
    assert response.error is None
    assert sandbox.files.read_calls == ["/proc/4321/fd/9"]
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "helper_code", "expected_error"),
    [
        ("upload", "invalid_path", "invalid_path"),
        ("download", "not_found", "file_not_found"),
        ("download", "permission_denied", "permission_denied"),
    ],
)
async def test_rooted_descriptor_maps_confirmed_target_errors(
    mode: str,
    helper_code: str,
    expected_error: str,
) -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    commands.helper_error = (helper_code, "confirmed target error")
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    if mode == "upload":
        response = await backend._aupload_rooted_file(
            root="/workspace",
            path="/target.bin",
            content=b"content",
        )
    else:
        response = await backend._adownload_rooted_file(
            root="/workspace",
            path="/target.bin",
        )

    assert response.path == "/target.bin"
    assert response.error == expected_error
    assert sandbox.files.write_calls == []
    assert sandbox.files.read_calls == []
    assert commands.interrupt_calls == []
    assert commands.status_calls == [commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_transfer_failure_still_settles_helper() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    sandbox.commands = commands
    sandbox.files.write_errors["/proc/4321/fd/9"] = PermissionError("read-only")
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(PermissionError, match="read-only"):
        await backend._aupload_rooted_file(
            root="/workspace",
            path="/target.bin",
            content=b"content",
        )

    assert len(sandbox.files.write_calls) == 1
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_upload_response_loss_is_not_replayed() -> None:
    class ResponseLossFiles(_FakeFiles):
        async def write_file(
            self,
            path: str,
            data: bytes,
            *,
            mode: int = 755,
        ) -> None:
            self.write_calls.append((path, data, mode))
            self.contents[path] = data
            raise ConnectionError("upload response lost")

    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    sandbox.commands = commands
    sandbox.files = ResponseLossFiles()
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
        await backend._aupload_rooted_file(
            root="/workspace",
            path="/target.bin",
            content=b"committed once",
        )
    assert isinstance(captured.value.cause, ConnectionError)
    assert str(captured.value.cause) == "upload response lost"

    assert sandbox.files.contents["/proc/4321/fd/9"] == b"committed once"
    assert sandbox.files.write_calls == [("/proc/4321/fd/9", b"committed once", 644)]
    assert commands.interrupt_calls == [commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_rejects_malformed_handshake_and_settles() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    commands.handshake_override = "not-json"
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(ValueError, match="handshake malformed"):
        await backend._adownload_rooted_file(
            root="/workspace",
            path="/target.bin",
        )

    assert sandbox.files.read_calls == []
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handshake",
    [
        json.dumps(
            {
                "version": "tinkerfin.rooted.transfer.v1",
                "token": "wrong-token",
                "mode": "download",
                "pid": 4321,
                "fd": 9,
            }
        ),
        json.dumps(
            {
                "version": "tinkerfin.rooted.transfer.v1",
                "token": "wrong-token",
                "mode": "upload",
                "pid": 0,
                "fd": 9,
            }
        ),
        "\n".join(
            [
                json.dumps(
                    {
                        "version": "tinkerfin.rooted.transfer.v1",
                        "token": "wrong-token",
                        "mode": "download",
                        "pid": 4321,
                        "fd": 9,
                    }
                )
            ]
            * 2
        ),
    ],
)
async def test_rooted_descriptor_rejects_mismatched_handshake(
    handshake: str,
) -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    commands.handshake_override = handshake
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(ValueError, match="handshake"):
        await backend._adownload_rooted_file(
            root="/workspace",
            path="/target.bin",
        )

    assert sandbox.files.read_calls == []
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_detects_helper_exit_before_handshake() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    commands.handshake_override = ""
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(ValueError, match="exited before handshake"):
        await backend._adownload_rooted_file(
            root="/workspace",
            path="/target.bin",
        )

    assert sandbox.files.read_calls == []
    assert commands.interrupt_calls == []
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_drains_final_logs_after_terminal_status() -> None:
    class DelayedTerminalLogs(_FakeDescriptorCommands):
        async def get_background_command_logs(
            self,
            execution_id: str,
            cursor: int | None = None,
        ) -> object:
            self.log_calls.append((execution_id, cursor))
            if len(self.log_calls) == 1:
                return SimpleNamespace(content="", cursor=0)
            return SimpleNamespace(content=self._handshake + "\n", cursor=1)

    sandbox = _FakeSandbox()
    commands = DelayedTerminalLogs()
    commands.helper_error = ("invalid_path", "confirmed delayed target error")
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    response = await backend._aupload_rooted_file(
        root="/workspace",
        path="/target.bin",
        content=b"content",
    )

    assert response.error == INVALID_PATH
    assert sandbox.files.write_calls == []
    assert commands.log_calls == [
        (commands.execution_id, None),
        (commands.execution_id, 0),
    ]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]
    assert commands.interrupt_calls == []


@pytest.mark.asyncio
async def test_rooted_descriptor_readiness_budget_bounds_hung_log_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungLogCommands(_FakeDescriptorCommands):
        async def get_background_command_logs(
            self,
            execution_id: str,
            cursor: int | None = None,
        ) -> object:
            self.log_calls.append((execution_id, cursor))
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(
        "tinkerfin_sandbox.backends.sdk._ROOTED_TRANSFER_READY_SECONDS",
        0.02,
    )
    sandbox = _FakeSandbox()
    commands = HungLogCommands()
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(TimeoutError):
        await backend._adownload_rooted_file(
            root="/workspace",
            path="/target.bin",
        )

    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_status_probe_leaves_budget_for_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FirstStatusHangs(_FakeDescriptorCommands):
        async def get_command_status(self, execution_id: str) -> object:
            self.status_calls.append(execution_id)
            if len(self.status_calls) == 1:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            return SimpleNamespace(
                id=execution_id,
                running=False,
                exit_code=143,
                error=None,
            )

    monkeypatch.setattr(
        "tinkerfin_sandbox.backends.sdk._ROOTED_TRANSFER_SETTLE_SECONDS",
        1.0,
    )
    sandbox = _FakeSandbox()
    commands = FirstStatusHangs()
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    response = await backend._aupload_rooted_file(
        root="/workspace",
        path="/target.bin",
        content=b"content",
    )

    assert response.error is None
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_surfaces_cleanup_failure_after_success() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    commands.interrupt_error = RuntimeError("interrupt failed")
    sandbox.commands = commands
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))

    with pytest.raises(RuntimeError, match="interrupt failed"):
        await backend._aupload_rooted_file(
            root="/workspace",
            path="/target.bin",
            content=b"content",
        )

    assert len(sandbox.files.write_calls) == 1
    assert commands.interrupt_calls == [commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_descriptor_cancellation_settles_without_replay() -> None:
    class BlockingFiles(_FakeFiles):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def write_file(
            self,
            path: str,
            data: bytes,
            *,
            mode: int = 755,
        ) -> None:
            self.write_calls.append((path, data, mode))
            self.started.set()
            await self.release.wait()

    sandbox = _FakeSandbox()
    commands = _FakeDescriptorCommands()
    files = BlockingFiles()
    sandbox.commands = commands
    sandbox.files = files
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))
    task = asyncio.create_task(
        backend._aupload_rooted_file(
            root="/workspace",
            path="/target.bin",
            content=b"content",
        )
    )
    await files.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert files.write_calls == [("/proc/4321/fd/9", b"content", 644)]
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_descriptor_settlement() -> None:
    class BlockingFiles(_FakeFiles):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def write_file(
            self,
            path: str,
            data: bytes,
            *,
            mode: int = 755,
        ) -> None:
            self.write_calls.append((path, data, mode))
            self.started.set()
            await asyncio.Event().wait()

    class BlockingInterruptCommands(_FakeDescriptorCommands):
        def __init__(self) -> None:
            super().__init__()
            self.interrupt_started = asyncio.Event()
            self.interrupt_release = asyncio.Event()

        async def interrupt(self, execution_id: str) -> None:
            self.interrupt_calls.append(execution_id)
            self.interrupt_started.set()
            await self.interrupt_release.wait()
            self.running = False

    sandbox = _FakeSandbox()
    commands = BlockingInterruptCommands()
    files = BlockingFiles()
    sandbox.commands = commands
    sandbox.files = files
    backend = OpenSandboxBackend(sandbox=cast(Sandbox, sandbox))
    task = asyncio.create_task(
        backend._aupload_rooted_file(
            root="/workspace",
            path="/target.bin",
            content=b"content",
        )
    )
    await files.started.wait()

    task.cancel()
    await commands.interrupt_started.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()

    commands.interrupt_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert commands.interrupt_calls == [commands.execution_id]
    assert commands.status_calls == [commands.execution_id, commands.execution_id]


@pytest.mark.asyncio
async def test_rooted_offload_sdk_parses_single_helper_result() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeOffloadCommands()
    sandbox.commands = commands
    backend = OpenSandboxBackend(
        sandbox=cast(Sandbox, sandbox),
        command_env={"CUSTOM": "value"},
        working_directory="/workspace",
        enable_capture_offload=True,
    )

    result = await backend._aexecute_rooted_offload(
        root="/workspace",
        command="generate-output",
        capture_path="/captures/call-1",
        max_inline_bytes=100,
        max_capture_bytes=200,
        timeout=3,
    )

    assert result.offloaded is True
    assert result.response == ExecuteResponse(
        output="preview",
        exit_code=7,
        truncated=True,
    )
    assert len(commands.calls) == 1
    assert commands.request is not None
    assert commands.request["arguments"] == {
        "path": "/captures/call-1",
        "command": "generate-output",
        "max_inline_bytes": 100,
        "max_capture_bytes": 200,
        "working_directory": "/workspace",
        "command_env": {"CUSTOM": "value"},
    }
    opts = cast(RunCommandOpts, commands.calls[0][1])
    assert opts.working_directory == "/"
    assert opts.envs == {
        "CUSTOM": "value",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PYTHONSAFEPATH": "1",
    }


@pytest.mark.asyncio
async def test_rooted_offload_malformed_result_never_reruns_command() -> None:
    sandbox = _FakeSandbox()
    commands = _FakeOffloadCommands()
    commands.malformed = True
    sandbox.commands = commands
    backend = OpenSandboxBackend(
        sandbox=cast(Sandbox, sandbox),
        enable_capture_offload=True,
    )

    with pytest.raises(ValueError, match="response malformed"):
        await backend._aexecute_rooted_offload(
            root="/workspace",
            command="run-once",
            capture_path="/captures/call-1",
            max_inline_bytes=100,
            max_capture_bytes=None,
            timeout=None,
        )

    assert len(commands.calls) == 1


@pytest.mark.asyncio
async def test_rooted_offload_disabled_executes_original_command_once() -> None:
    sandbox = _FakeSandbox()
    backend = OpenSandboxBackend(
        sandbox=cast(Sandbox, sandbox),
        enable_capture_offload=False,
    )

    result = await backend._aexecute_rooted_offload(
        root="/workspace",
        command="run-inline",
        capture_path="/captures/call-1",
        max_inline_bytes=100,
        max_capture_bytes=None,
        timeout=3,
    )

    assert result.offloaded is False
    assert sandbox.commands.calls[0][0] == "run-inline"
    assert len(sandbox.commands.calls) == 1


class OpenSandboxClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection_config = ConnectionConfig(request_timeout=timedelta(minutes=15))
        self.config = OpenSandboxConfig(
            image="registry.example/sandbox:1",
            entrypoint=["/entrypoint.sh"],
            env={"PYTHON_VERSION": "3.11"},
            metadata={"team": "agents"},
            resource={"cpu": "2", "memory": "4Gi"},
            ttl=timedelta(hours=2),
            ready_timeout=timedelta(minutes=5),
            connect_timeout=timedelta(seconds=20),
            command_timeout=90,
            health_command="printf ok",
            warm_pool_size=0,
            command_env={"PATH": "/opt/bin:/usr/bin"},
        )

    async def test_uses_safe_lifecycle_timeout_when_sdk_default_is_implicit(
        self,
    ) -> None:
        connection = ConnectionConfig(domain="sandbox.example.com")
        client = OpenSandboxClient(
            connection_config=connection,
            config=self.config,
        )

        self.assertIsNot(client.connection_config, connection)
        self.assertEqual(
            client.connection_config.request_timeout,
            self.config.lifecycle_request_timeout,
        )
        self.assertEqual(client.connection_config.domain, "sandbox.example.com")

    async def test_preserves_explicit_lifecycle_request_timeout(self) -> None:
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        self.assertIs(client.connection_config, self.connection_config)
        self.assertEqual(
            client.connection_config.request_timeout,
            timedelta(minutes=15),
        )

    async def test_create_passes_configuration_and_runs_initializers(self) -> None:
        sandbox = _FakeSandbox()
        initializer = MagicMock()
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
            initializers=[initializer],
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
            return_value=sandbox,
        ) as create:
            backend = await client.create()

        self.assertEqual(backend.id, sandbox.id)
        initializer.assert_called_once_with(backend)
        kwargs = create.call_args.kwargs
        self.assertEqual(create.call_args.args, (self.config.image,))
        self.assertEqual(kwargs["entrypoint"], self.config.entrypoint)
        self.assertEqual(kwargs["env"], self.config.env)
        metadata = dict(kwargs["metadata"])
        create_token = metadata.pop("tinkerfin.ai/create-token")
        self.assertEqual(metadata, self.config.metadata)
        self.assertEqual(len(create_token), 32)
        self.assertEqual(kwargs["resource"], self.config.resource)
        self.assertEqual(kwargs["timeout"], self.config.ttl)
        self.assertEqual(kwargs["ready_timeout"], self.config.ready_timeout)
        self.assertIs(kwargs["connection_config"], self.connection_config)

    async def test_create_uses_native_async_sdk_and_passes_volumes(self) -> None:
        volume = Volume(
            name="workspace-data",
            pvc=PVC(claimName="workspace-pvc"),
            mountPath="/workspace/data",
        )
        sandbox = _FakeSandbox()
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config.model_copy(update={"volumes": (volume,)}),
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
                return_value=sandbox,
            ) as create,
            patch(
                "tinkerfin_sandbox.lifecycle.client.asyncio.to_thread",
                side_effect=AssertionError("native async path must not use to_thread"),
            ),
        ):
            backend = await client.create()

        self.assertEqual(backend.id, sandbox.id)
        passed_volumes = create.call_args.kwargs["volumes"]
        self.assertEqual(passed_volumes, [volume])
        self.assertIsNot(passed_volumes[0], volume)

    async def test_create_destroys_new_sandbox_when_initializer_fails(self) -> None:
        sandbox = _FakeSandbox()
        initializer = MagicMock(side_effect=RuntimeError("seed failed"))
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
            initializers=[initializer],
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
                return_value=sandbox,
            ),
            self.assertRaisesRegex(RuntimeError, "seed failed"),
        ):
            await client.create()

        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)

    async def test_cancelled_create_reclaims_sandbox_created_by_native_task(
        self,
    ) -> None:
        sandbox = _FakeSandbox()
        create_entered = asyncio.Event()
        create_gate = asyncio.Event()

        async def delayed_create(
            *_args: object,
            **_kwargs: object,
        ) -> _FakeSandbox:
            create_entered.set()
            await create_gate.wait()
            return sandbox

        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )
        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
            side_effect=delayed_create,
        ):
            create_task = asyncio.create_task(client.create())
            await asyncio.wait_for(create_entered.wait(), timeout=1)
            create_task.cancel()
            create_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await create_task

        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)

    async def test_initializer_error_is_not_masked_by_cleanup_failures(self) -> None:
        sandbox = _FakeSandbox()
        sandbox.kill_error = RuntimeError("kill failed")
        sandbox.close_error = RuntimeError("close failed")
        initializer = MagicMock(side_effect=RuntimeError("seed failed"))
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
            initializers=[initializer],
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
                return_value=sandbox,
            ),
            self.assertLogs("tinkerfin_sandbox.lifecycle.client", level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "seed failed"),
        ):
            await client.create()

        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)

    async def test_connect_is_strict_and_closes_on_initializer_failure(self) -> None:
        sandbox = _FakeSandbox("existing")
        initializer = MagicMock(side_effect=RuntimeError("restore failed"))
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
            initializers=[initializer],
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
                return_value=sandbox,
            ) as connect,
            self.assertRaisesRegex(RuntimeError, "restore failed"),
        ):
            await client.connect("existing")

        connect.assert_called_once_with(
            "existing",
            connection_config=self.connection_config,
            connect_timeout=self.config.connect_timeout,
        )
        self.assertTrue(sandbox.closed)
        self.assertFalse(sandbox.killed)

    async def test_cancelled_connect_closes_backend_created_by_native_task(
        self,
    ) -> None:
        sandbox = _FakeSandbox("existing")
        connect_entered = asyncio.Event()
        connect_gate = asyncio.Event()
        connect_finished = asyncio.Event()

        async def delayed_connect(
            *_args: object,
            **_kwargs: object,
        ) -> _FakeSandbox:
            connect_entered.set()
            await connect_gate.wait()
            connect_finished.set()
            return sandbox

        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )
        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            side_effect=delayed_connect,
        ):
            connect_task = asyncio.create_task(client.connect("existing"))
            await asyncio.wait_for(connect_entered.wait(), timeout=1)
            connect_task.cancel()
            connect_gate.set()
            await asyncio.wait_for(connect_finished.wait(), timeout=1)
            with self.assertRaises(asyncio.CancelledError):
                await connect_task

        self.assertTrue(sandbox.closed)
        self.assertFalse(sandbox.killed)

    async def test_inspect_returns_stable_schema_and_closes_temporary_connection(
        self,
    ) -> None:
        sandbox = _FakeSandbox("existing")
        sandbox.commands.result = SimpleNamespace(
            exit_code=0,
            logs=SimpleNamespace(stdout=[SimpleNamespace(text="ok")], stderr=[]),
        )
        initializer = MagicMock()
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
            initializers=[initializer],
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            return_value=sandbox,
        ):
            details = await client.inspect("existing")

        self.assertIsInstance(details, OpenSandboxRuntimeInfo)
        self.assertEqual(details.sandbox_id, "existing")
        self.assertTrue(details.available)
        self.assertTrue(details.healthy)
        assert details.status is not None
        self.assertEqual(details.status.state, "RUNNING")
        self.assertEqual(details.image, "registry.example/sandbox:1")
        self.assertEqual(details.metadata, {"purpose": "test"})
        self.assertTrue(sandbox.closed)
        initializer.assert_not_called()

    async def test_inspect_result_is_not_masked_by_local_close_failure(self) -> None:
        sandbox = _FakeSandbox("existing")
        sandbox.close_error = RuntimeError("close failed")
        sandbox.commands.result = SimpleNamespace(
            exit_code=0,
            logs=SimpleNamespace(stdout=[SimpleNamespace(text="ok")], stderr=[]),
        )
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
                return_value=sandbox,
            ),
            self.assertLogs("tinkerfin_sandbox.lifecycle.client", level="WARNING"),
        ):
            details = await client.inspect("existing")

        self.assertTrue(details.available)
        self.assertTrue(sandbox.closed)

    async def test_inspect_maps_unreachable_sandbox_without_raising(self) -> None:
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            side_effect=RuntimeError("sandbox not found"),
        ):
            details = await client.inspect("missing")

        self.assertEqual(details.sandbox_id, "missing")
        self.assertFalse(details.available)
        self.assertFalse(details.healthy)
        self.assertEqual(details.unavailable_reason, "not_found")

    async def test_destroy_kills_and_closes_temporary_connection(self) -> None:
        sandbox = _FakeSandbox("existing")
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            return_value=sandbox,
        ) as connect:
            await client.destroy("existing")

        connect.assert_called_once_with(
            "existing",
            connection_config=self.connection_config,
            connect_timeout=self.config.connect_timeout,
            skip_health_check=True,
        )
        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)

    async def test_destroy_closes_connection_even_when_kill_fails(self) -> None:
        sandbox = _FakeSandbox("existing")
        sandbox.kill = AsyncMock(side_effect=RuntimeError("kill failed"))
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            return_value=sandbox,
        ):
            with self.assertRaises(UnexpectedOpenSandboxBackendError) as captured:
                await client.destroy("existing")

        self.assertIsInstance(captured.exception.cause, RuntimeError)
        self.assertEqual(str(captured.exception.cause), "kill failed")
        self.assertTrue(sandbox.closed)
        self.assertEqual(sandbox.kill.call_args_list, [call()])

    async def test_destroy_preserves_kill_error_when_close_also_fails(self) -> None:
        sandbox = _FakeSandbox("existing")
        sandbox.kill_error = RuntimeError("kill failed")
        sandbox.close_error = RuntimeError("close failed")
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
                return_value=sandbox,
            ),
            self.assertLogs("tinkerfin_sandbox.lifecycle.client", level="WARNING"),
        ):
            with self.assertRaises(UnexpectedOpenSandboxBackendError) as captured:
                await client.destroy("existing")

        self.assertIsInstance(captured.exception.cause, RuntimeError)
        self.assertEqual(str(captured.exception.cause), "kill failed")
        self.assertTrue(sandbox.closed)

    async def test_destroy_retains_kill_and_close_after_caller_cancellation(
        self,
    ) -> None:
        sandbox = _FakeSandbox("existing")
        kill_started = asyncio.Event()
        kill_release = asyncio.Event()

        async def delayed_kill() -> None:
            kill_started.set()
            await kill_release.wait()
            sandbox.killed = True

        sandbox.kill = delayed_kill
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            return_value=sandbox,
        ):
            destroying = asyncio.create_task(client.destroy("existing"))
            await asyncio.wait_for(kill_started.wait(), timeout=1)
            destroying.cancel("caller stopped waiting")
            await asyncio.sleep(0)
            self.assertFalse(destroying.done())
            kill_release.set()
            with self.assertRaisesRegex(
                asyncio.CancelledError,
                "caller stopped waiting",
            ):
                await asyncio.wait_for(destroying, timeout=1)

        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)

    async def test_destroy_logs_local_close_failure_after_confirmed_kill(self) -> None:
        sandbox = _FakeSandbox("existing")
        sandbox.close_error = RuntimeError("close failed")
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with (
            patch(
                "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
                return_value=sandbox,
            ),
            self.assertLogs("tinkerfin_sandbox.lifecycle.client", level="WARNING"),
        ):
            await client.destroy("existing")

        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)

    async def test_destroy_treats_missing_remote_sandbox_as_already_deleted(
        self,
    ) -> None:
        client = OpenSandboxClient(
            connection_config=self.connection_config,
            config=self.config,
        )

        with patch(
            "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
            side_effect=RuntimeError("sandbox not found"),
        ):
            await client.destroy("missing")


@pytest.mark.asyncio
async def test_execute_uses_configured_working_directory() -> None:
    sandbox = _FakeSandbox()
    backend = OpenSandboxBackend(
        sandbox=cast(Sandbox, sandbox),
        working_directory="/workspace",
    )

    await backend.aexecute("pwd")

    opts = cast(RunCommandOpts, sandbox.commands.calls[0][1])
    assert opts.working_directory == "/workspace"


@pytest.mark.asyncio
async def test_rooted_file_operation_uses_isolated_internal_command_context() -> None:
    sandbox = _FakeSandbox()
    backend = OpenSandboxBackend(
        sandbox=cast(Sandbox, sandbox),
        command_env={"PROJECT_ENV": "enabled", "PYTHONPATH": "/workspace"},
        working_directory="/workspace",
    )

    with backend._rooted_file_operation():
        await backend.aexecute("python3 -c 'print(1)'")
    await backend.aexecute("pwd")

    internal_opts = cast(RunCommandOpts, sandbox.commands.calls[0][1])
    user_opts = cast(RunCommandOpts, sandbox.commands.calls[1][1])
    assert internal_opts.working_directory == "/"
    assert internal_opts.envs == {
        "PROJECT_ENV": "enabled",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PYTHONSAFEPATH": "1",
    }
    assert user_opts.working_directory == "/workspace"
    assert user_opts.envs == {
        "PROJECT_ENV": "enabled",
        "PYTHONPATH": "/workspace",
    }


@pytest.mark.asyncio
async def test_create_initializes_workspace_before_custom_initializers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox()
    observed_directories: list[list[str]] = []

    def observe_workspace(_backend: OpenSandboxBackend) -> None:
        observed_directories.append(list(sandbox.files.created_directories))

    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(return_value=sandbox),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
        initializers=[observe_workspace],
    )

    await client.create()

    assert observed_directories == [["/workspace"]]
    assert sandbox.files.created_directory_modes == [755]


@pytest.mark.asyncio
async def test_connect_initializes_workspace_before_custom_initializers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox("existing")
    observed_directories: list[list[str]] = []

    def observe_workspace(_backend: OpenSandboxBackend) -> None:
        observed_directories.append(list(sandbox.files.created_directories))

    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        AsyncMock(return_value=sandbox),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
        initializers=[observe_workspace],
    )

    await client.connect("existing")

    assert observed_directories == [["/workspace"]]


@pytest.mark.asyncio
async def test_inspect_does_not_initialize_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox("existing")
    sandbox.commands.result = SimpleNamespace(
        exit_code=0,
        logs=SimpleNamespace(stdout=[SimpleNamespace(text="ok")], stderr=[]),
    )
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        AsyncMock(return_value=sandbox),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    details = await client.inspect("existing")

    assert details.healthy
    assert sandbox.files.created_directories == []


@pytest.mark.asyncio
async def test_workspace_root_none_disables_initialization_and_shell_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox()
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(return_value=sandbox),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(workspace_root=None, warm_pool_size=0),
    )

    backend = await client.create()
    await backend.aexecute("pwd")

    opts = cast(RunCommandOpts, sandbox.commands.calls[0][1])
    assert sandbox.files.created_directories == []
    assert opts.working_directory is None


@pytest.mark.asyncio
async def test_workspace_initialization_failure_reclaims_new_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox()
    sandbox.files.create_directories_error = RuntimeError("mkdir failed")
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(return_value=sandbox),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
        await client.create()

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "mkdir failed"
    assert sandbox.killed
    assert sandbox.closed


@pytest.mark.asyncio
async def test_workspace_initialization_failure_only_closes_reconnected_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox("existing")
    sandbox.files.create_directories_error = RuntimeError("mkdir failed")
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        AsyncMock(return_value=sandbox),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
        await client.connect("existing")

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "mkdir failed"
    assert sandbox.closed
    assert not sandbox.killed


@pytest.mark.asyncio
async def test_create_merges_per_call_metadata_and_adds_request_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeSandbox()
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        create,
    )
    owner_metadata = {"tinkerfin.ai/owner": "a" * 43}
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(
            metadata={"team": "agents"},
            warm_pool_size=0,
        ),
    )

    await client.create(metadata=owner_metadata)

    passed = create.call_args.kwargs["metadata"]
    assert passed["team"] == "agents"
    assert passed["tinkerfin.ai/owner"] == "a" * 43
    assert len(passed["tinkerfin.ai/create-token"]) == 32
    assert owner_metadata == {"tinkerfin.ai/owner": "a" * 43}


@pytest.mark.asyncio
async def test_create_recovers_one_candidate_after_unknown_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "00000000000000000000000000000001"
    sdk_manager = _FakeOpenSandboxSDKManager(
        [
            _sandbox_info(
                "recovered-sandbox",
                metadata={"tinkerfin.ai/create-token": token},
            )
        ]
    )
    recovered = _FakeSandbox("recovered-sandbox")
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.uuid4",
        lambda: UUID(int=1),
    )
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(side_effect=RuntimeError("response lost")),
    )
    connect = AsyncMock(return_value=recovered)
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        connect,
    )
    monkeypatch.setattr(
        OpenSandboxSDKManager,
        "create",
        AsyncMock(return_value=sdk_manager),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    backend = await client.create()

    assert backend.id == "recovered-sandbox"
    assert sdk_manager.filters == [
        SandboxFilter(
            metadata={"tinkerfin.ai/create-token": token},
            page_size=2,
            page=1,
        )
    ]
    assert sdk_manager.killed_ids == []
    assert sdk_manager.closed
    connect.assert_awaited_once_with(
        "recovered-sandbox",
        connection_config=client.connection_config,
        connect_timeout=client.config.connect_timeout,
    )


@pytest.mark.asyncio
async def test_create_preserves_original_error_when_discovery_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "00000000000000000000000000000001"
    sdk_manager = _FakeOpenSandboxSDKManager([])
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.uuid4",
        lambda: UUID(int=1),
    )
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(side_effect=RuntimeError("response lost")),
    )
    connect = AsyncMock()
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        connect,
    )
    monkeypatch.setattr(
        OpenSandboxSDKManager,
        "create",
        AsyncMock(return_value=sdk_manager),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
        await client.create()

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "response lost"
    assert sdk_manager.filters == [
        SandboxFilter(
            metadata={"tinkerfin.ai/create-token": token},
            page_size=2,
            page=1,
        )
    ]
    assert sdk_manager.closed
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_preserves_original_error_when_discovery_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_manager = _FakeOpenSandboxSDKManager([])
    sdk_manager.close_error = RuntimeError("manager close failed")
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(side_effect=RuntimeError("response lost")),
    )
    monkeypatch.setattr(
        OpenSandboxSDKManager,
        "create",
        AsyncMock(return_value=sdk_manager),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
        await client.create()

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "response lost"
    assert sdk_manager.closed


@pytest.mark.asyncio
async def test_create_rejects_and_cleans_multiple_discovered_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "00000000000000000000000000000001"
    sdk_manager = _FakeOpenSandboxSDKManager(
        [
            _sandbox_info(
                "duplicate-1",
                metadata={"tinkerfin.ai/create-token": token},
            ),
            _sandbox_info(
                "duplicate-2",
                metadata={"tinkerfin.ai/create-token": token},
            ),
            _sandbox_info(
                "duplicate-3",
                metadata={"tinkerfin.ai/create-token": token},
            ),
        ]
    )
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.uuid4",
        lambda: UUID(int=1),
    )
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.create",
        AsyncMock(side_effect=RuntimeError("response lost")),
    )
    connect = AsyncMock()
    monkeypatch.setattr(
        "tinkerfin_sandbox.lifecycle.client.Sandbox.connect",
        connect,
    )
    monkeypatch.setattr(
        OpenSandboxSDKManager,
        "create",
        AsyncMock(return_value=sdk_manager),
    )
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(),
        config=OpenSandboxConfig(warm_pool_size=0),
    )

    with pytest.raises(UnexpectedOpenSandboxBackendError) as captured:
        await client.create()

    assert isinstance(captured.value.cause, RuntimeError)
    assert str(captured.value.cause) == "response lost"
    assert sdk_manager.filters == [
        SandboxFilter(
            metadata={"tinkerfin.ai/create-token": token},
            page_size=2,
            page=1,
        ),
        SandboxFilter(
            metadata={"tinkerfin.ai/create-token": token},
            page_size=2,
            page=2,
        ),
    ]
    assert sdk_manager.killed_ids == [
        "duplicate-1",
        "duplicate-2",
        "duplicate-3",
    ]
    assert sdk_manager.closed
    connect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
