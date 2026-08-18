"""Deep Agents virtual-root view over a managed OpenSandbox handle."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shlex
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, NoReturn, TypeVar

from deepagents.backends.protocol import (
    ASYNC_GREP_TIMEOUT,
    INVALID_PATH,
    DeleteResult,
    EditResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.utils import normalize_read_bounds

from ..models import OpenSandboxRuntimeInfo, _normalize_workspace_root
from ._rooted_protocol import (
    _build_rooted_command,
    _parse_rooted_response,
    _RootedCommand,
)
from .handle import OpenSandboxHandle
from .sdk import OpenSandboxBackend

logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT")
_ROOTED_BINARY_READ_SUFFIXES = frozenset(
    {
        ".aac",
        ".aiff",
        ".avi",
        ".flac",
        ".flv",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".wav",
        ".webm",
        ".webp",
        ".wmv",
        ".3gpp",
    }
)
_ROOTED_EDIT_INLINE_MAX_BYTES = 50_000


@dataclass(frozen=True, slots=True)
class _MappedPath:
    """Store one virtual path input and its normalized Sandbox path."""

    requested: str
    virtual: str
    physical: str


@dataclass(slots=True)
class _AsyncStartState:
    """Track whether a native async operation has pinned a Handle lease."""

    has_started: bool = False


class RootedOpenSandboxBackend(BaseSandbox):
    """Project model-visible virtual paths into a fixed Sandbox workspace.

    The view owns no remote lifecycle. Native OpenSandbox remote I/O is asynchronous;
    synchronous protocol methods propagate the backend's explicit async-only error.
    """

    def __init__(
        self,
        handle: OpenSandboxHandle,
        *,
        root: str = "/workspace",
    ) -> None:
        """Create a virtual-root view without taking remote lifecycle ownership.

        Args:
            handle: Stable Sandbox handle owned by ``OpenSandboxManager``.
            root: Absolute POSIX directory represented by the model-visible root.

        Raises:
            ValueError: ``root`` is not a safe non-root absolute POSIX path.
        """
        normalized_root = _normalize_workspace_root(root)
        if normalized_root is None:
            raise ValueError("root must be a safe non-root absolute POSIX path")
        self._handle = handle
        self._root = normalized_root
        self._root_error_pattern = re.compile(
            rf"(?<![\w./-]){re.escape(self._root)}"
            r"(?:/|(?=$|[\s'\".,:;)\]}]))"
        )
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def id(self) -> str:
        """Return the remote Sandbox ID currently held by the stable handle."""
        return self._handle.id

    @property
    def enable_capture_offload(self) -> bool:
        """Return whether the current backend can offload captured output."""
        return self._handle.enable_capture_offload

    @property
    def is_closed(self) -> bool:
        """Return whether the lifecycle manager has closed the stable handle."""
        return self._handle.is_closed

    def close(self) -> None:
        """Reject closure that bypasses the lifecycle manager."""
        self._handle.close()

    def renew(self, timeout: timedelta) -> None:
        """Extend the current remote Sandbox lifetime through the stable handle."""

        self._handle.renew(timeout)

    async def arenew(self, timeout: timedelta) -> None:
        """Asynchronously extend the current remote Sandbox lifetime."""

        await self._handle.arenew(timeout)

    def get_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Return runtime details for the Sandbox currently held by the handle."""

        return self._handle.get_runtime_info()

    async def aget_runtime_info(self) -> OpenSandboxRuntimeInfo:
        """Asynchronously return runtime details for the current Sandbox."""

        return await self._handle.aget_runtime_info()

    def _start_async_task(
        self,
        operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]],
    ) -> tuple[asyncio.Task[_ResultT], _AsyncStartState]:
        """Pin a backend in an owned task until the remote call settles."""
        state = _AsyncStartState()

        async def run() -> _ResultT:
            async with self._handle._alease() as backend:
                state.has_started = True
                return await operation(backend)

        task = asyncio.create_task(run())
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_async_task)
        return task, state

    def _finish_async_task(self, task: asyncio.Task[Any]) -> None:
        """Retain a background task and consume errors after caller cancellation."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        _ = task.exception()

    async def _run_async(
        self,
        operation: Callable[[OpenSandboxBackend], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Await native async I/O without abandoning a started Handle lease."""
        task, state = self._start_async_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not state.has_started:
                task.cancel()
            raise

    @contextmanager
    def _lease_backend(self) -> Iterator[OpenSandboxBackend]:
        """Pin the Handle backend for one synchronous protocol call."""
        with self._handle._lease() as backend:
            yield backend

    def _reject_sync_remote_io(self) -> NoReturn:
        """Reject sync paths before any remote filesystem operation."""
        with self._lease_backend():
            OpenSandboxBackend._reject_sync()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a Shell command unchanged; the virtual root scopes file tools."""
        with self._lease_backend() as backend:
            return backend.execute(command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a Shell command unchanged through the native async backend."""

        async def operation(backend: OpenSandboxBackend) -> ExecuteResponse:
            return await backend.aexecute(command, timeout=timeout)

        return await self._run_async(operation)

    def to_shell_path(self, file_path: str) -> str:
        """Convert a virtual file-tool path to a workspace-relative Shell path."""

        mapped = self._map_path(file_path)
        if mapped is None:
            raise ValueError("file_path must be a safe virtual path")
        return mapped.virtual.lstrip("/") or "."

    def _map_path(self, path: str) -> _MappedPath | None:
        """Normalize a caller path into virtual and physical Sandbox paths."""
        if "\x00" in path or path.startswith("//"):
            return None
        virtual_path = PurePosixPath(path)
        if ".." in virtual_path.parts:
            return None

        relative_parts = tuple(
            part for part in virtual_path.parts if part not in {"/", "."}
        )
        normalized_virtual = "/" + "/".join(relative_parts)
        physical = str(PurePosixPath(self._root, *relative_parts))
        return _MappedPath(
            requested=path,
            virtual=normalized_virtual,
            physical=physical,
        )

    @staticmethod
    def _display_input(path: str) -> str:
        """Escape NUL bytes while preserving other model input for diagnostics."""
        return path.replace("\x00", "\\0")

    def _invalid_path_error(self, path: str) -> str:
        """Return a stable error that reveals no physical root or symlink target."""
        return f"Path '{self._display_input(path)}': {INVALID_PATH}"

    @staticmethod
    def _read_as_binary(path: str) -> bool:
        """Match Deep Agents' non-text extension classification."""
        return PurePosixPath(path).suffix.lower() in _ROOTED_BINARY_READ_SUFFIXES

    def _project_read_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> ReadResult:
        """Translate one validated helper envelope into ``ReadResult``."""
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error":
            if parsed.error.code == "invalid_path":
                return ReadResult(error=self._invalid_path_error(requested.requested))
            message = {
                "not_found": "file_not_found",
                "permission_denied": "permission_denied",
                "not_a_file": "not_a_file",
                "not_directory": "not_a_file",
            }.get(parsed.error.code, parsed.error.message)
            return ReadResult(error=f"File '{requested.virtual}': {message}")
        if parsed.operation != "read":
            raise ValueError("rooted helper returned a non-read result")
        result = parsed.result
        return ReadResult(
            file_data={
                "content": result["content"],
                "encoding": result["encoding"],
            },
            total_lines=result["total_lines"],
            start_line=result["start_line"],
            end_line=result["end_line"],
            next_offset=result["next_offset"],
            no_lines_requested=result["no_lines_requested"],
        )

    def _project_edit_response(
        self,
        *,
        requested: _MappedPath,
        old_string: str,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> EditResult:
        """Translate one validated helper envelope into ``EditResult``."""
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error":
            code = parsed.error.code
            if code == "invalid_path":
                return EditResult(error=self._invalid_path_error(requested.requested))
            messages = {
                "not_found": f"Error: File '{requested.virtual}' not found",
                "permission_denied": (
                    f"Error: Permission denied editing file '{requested.virtual}'"
                ),
                "not_a_file": f"Error: '{requested.virtual}' is not a regular file",
                "not_text_file": (
                    f"Error: File '{requested.virtual}' is not a text file"
                ),
                "string_not_found": (
                    f"Error: String not found in file: '{old_string}'"
                ),
                "multiple_occurrences": (
                    f"Error: String '{old_string}' appears multiple times. "
                    "Use replace_all=True to replace all occurrences."
                ),
            }
            return EditResult(
                error=messages.get(
                    code,
                    f"Error editing file '{requested.virtual}': {parsed.error.message}",
                )
            )
        if parsed.operation != "edit":
            raise ValueError("rooted helper returned a non-edit result")
        return EditResult(
            path=requested.requested,
            occurrences=parsed.result["count"],
        )

    @staticmethod
    def _edit_staging_paths() -> tuple[str, str]:
        """Allocate unguessable Sandbox paths for an oversized edit payload."""
        token = secrets.token_hex(16)
        prefix = f"/tmp/.tinkerfin-rooted-edit-{token}"
        return f"{prefix}-old", f"{prefix}-new"

    @staticmethod
    def _edit_upload_error(
        *,
        requested: _MappedPath,
        responses: list[FileUploadResponse],
    ) -> EditResult | None:
        """Project staged edit upload failures without starting the target edit."""
        if len(responses) != 2:
            return EditResult(
                error=(
                    f"Error editing file '{requested.virtual}': "
                    "upload returned no response"
                )
            )
        for response in responses:
            if response.error is not None:
                return EditResult(
                    error=(
                        f"Error editing file '{requested.virtual}': {response.error}"
                    )
                )
        return None

    @staticmethod
    def _edit_cleanup_command(old_path: str, new_path: str) -> str:
        """Build best-effort cleanup for generated staging paths only."""
        return f"rm -f {shlex.quote(old_path)} {shlex.quote(new_path)}"

    def _cleanup_edit_staging(
        self,
        backend: OpenSandboxBackend,
        *,
        old_path: str,
        new_path: str,
    ) -> None:
        """Best-effort cleanup without replaying the target edit."""
        try:
            cleanup = backend.execute(self._edit_cleanup_command(old_path, new_path))
        except Exception:
            logger.warning(
                "Failed to clean up staged Rooted edit payload", exc_info=True
            )
            return
        if cleanup.exit_code != 0:
            logger.warning(
                "Failed to clean up staged Rooted edit payload: %s",
                cleanup.output[:200],
            )

    async def _acleanup_edit_staging(
        self,
        backend: OpenSandboxBackend,
        *,
        old_path: str,
        new_path: str,
    ) -> None:
        """Asynchronously clean generated staging paths without replaying edit."""
        try:
            cleanup = await backend.aexecute(
                self._edit_cleanup_command(old_path, new_path)
            )
        except Exception:
            logger.warning(
                "Failed to clean up staged Rooted edit payload", exc_info=True
            )
            return
        if cleanup.exit_code != 0:
            logger.warning(
                "Failed to clean up staged Rooted edit payload: %s",
                cleanup.output[:200],
            )

    def _project_delete_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> DeleteResult:
        """Translate one validated helper envelope into ``DeleteResult``."""
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error":
            if parsed.error.code == "invalid_path":
                return DeleteResult(error=self._invalid_path_error(requested.requested))
            if parsed.error.code == "not_found":
                return DeleteResult(error=f"Error: '{requested.virtual}' not found")
            return DeleteResult(
                error=(
                    f"Error deleting file '{requested.virtual}': {parsed.error.message}"
                )
            )
        if parsed.operation != "delete":
            raise ValueError("rooted helper returned a non-delete result")
        return DeleteResult(path=requested.requested)

    def _project_list_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> LsResult:
        """Translate one validated helper envelope into ``LsResult``."""
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error":
            if parsed.error.code == "invalid_path":
                return LsResult(
                    error=self._invalid_path_error(requested.requested),
                    entries=None,
                )
            message = {
                "not_found": "path_not_found",
                "not_directory": "not_a_directory",
                "permission_denied": "permission_denied",
            }.get(parsed.error.code, parsed.error.message)
            return LsResult(
                error=f"Path '{requested.virtual}': {message}",
                entries=None,
            )
        if parsed.operation != "list":
            raise ValueError("rooted helper returned a non-list result")
        entries: list[FileInfo] = [
            {"path": entry["path"], "is_dir": entry["is_dir"]}
            for entry in parsed.result["entries"]
        ]
        return LsResult(
            error=self._restore_error(parsed.result["partial_error"]),
            entries=entries,
        )

    def _project_glob_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> GlobResult:
        """Translate one validated helper envelope into ``GlobResult``."""
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error":
            if parsed.error.code == "invalid_path":
                return GlobResult(
                    error=self._invalid_path_error(requested.requested),
                    matches=None,
                )
            message = {
                "not_found": "path_not_found",
                "not_directory": "not_a_directory",
                "permission_denied": "permission_denied",
            }.get(parsed.error.code, parsed.error.message)
            return GlobResult(
                error=f"Path '{requested.virtual}': {message}",
                matches=None,
            )
        if parsed.operation != "glob":
            raise ValueError("rooted helper returned a non-glob result")
        matches: list[FileInfo] = [
            {"path": match["path"], "is_dir": match["is_dir"]}
            for match in parsed.result["matches"]
        ]
        return GlobResult(
            error=self._restore_error(parsed.result["partial_error"]),
            matches=matches,
            truncated=parsed.result["truncated"],
        )

    def _project_grep_response(
        self,
        *,
        requested: _MappedPath,
        response: ExecuteResponse,
        request: _RootedCommand,
    ) -> GrepResult:
        """Translate one validated helper envelope into ``GrepResult``."""
        parsed = _parse_rooted_response(response, request=request)
        if parsed.status == "error":
            if parsed.error.code == "invalid_path":
                return GrepResult(
                    error=self._invalid_path_error(requested.requested),
                    matches=None,
                )
            message = {
                "not_found": "path_not_found",
                "permission_denied": "permission_denied",
            }.get(parsed.error.code, parsed.error.message)
            return GrepResult(
                error=f"Path '{requested.virtual}': {message}",
                matches=None,
            )
        if parsed.operation != "grep":
            raise ValueError("rooted helper returned a non-grep result")
        matches: list[GrepMatch] = [
            {
                "path": match["path"],
                "line": match["line"],
                "text": match["text"],
            }
            for match in parsed.result["matches"]
        ]
        return GrepResult(
            error=self._restore_error(parsed.result["partial_error"]),
            matches=matches,
            truncated=parsed.result["truncated"],
        )

    def _restore_error(self, error: str | None) -> str | None:
        """Replace physical workspace prefixes in errors with the virtual root."""
        if error is None:
            return None
        return self._root_error_pattern.sub("/", error)

    @staticmethod
    def _is_safe_path_pattern(pattern: str) -> bool:
        """Return whether a glob pattern avoids traversal and NUL bytes."""
        return "\x00" not in pattern and ".." not in PurePosixPath(pattern).parts

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read a text or binary preview within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return ReadResult(error=self._invalid_path_error(file_path))
        offset, limit = normalize_read_bounds(offset, limit)
        request = _build_rooted_command(
            root=self._root,
            operation="read",
            arguments={
                "path": mapped.virtual,
                "offset": offset,
                "limit": limit,
                "binary": self._read_as_binary(mapped.virtual),
            },
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_read_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Asynchronously read a file within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return ReadResult(error=self._invalid_path_error(file_path))
        offset, limit = normalize_read_bounds(offset, limit)
        request = _build_rooted_command(
            root=self._root,
            operation="read",
            arguments={
                "path": mapped.virtual,
                "offset": offset,
                "limit": limit,
                "binary": self._read_as_binary(mapped.virtual),
            },
        )

        async def operation(backend: OpenSandboxBackend) -> ReadResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_read_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Reject synchronous remote writes; use :meth:`awrite`."""
        del content
        mapped = self._map_path(file_path)
        if mapped is None:
            return WriteResult(error=self._invalid_path_error(file_path))
        self._reject_sync_remote_io()

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Asynchronously write a text file within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return WriteResult(error=self._invalid_path_error(file_path))

        async def operation(backend: OpenSandboxBackend) -> WriteResult:
            result = await backend._aupload_rooted_file(
                root=self._root,
                path=mapped.virtual,
                content=content.encode("utf-8"),
            )
            if result.error == INVALID_PATH:
                return WriteResult(error=self._invalid_path_error(file_path))
            if result.error is not None:
                return WriteResult(
                    error=(
                        f"Failed to write file '{mapped.virtual}': "
                        f"{self._restore_error(result.error)}"
                    )
                )
            return WriteResult(path=mapped.requested)

        return await self._run_async(operation)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Apply an exact text replacement within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return EditResult(error=self._invalid_path_error(file_path))
        payload_size = len(old_string.encode("utf-8")) + len(new_string.encode("utf-8"))
        if payload_size <= _ROOTED_EDIT_INLINE_MAX_BYTES:
            request = _build_rooted_command(
                root=self._root,
                operation="edit",
                arguments={
                    "path": mapped.virtual,
                    "old": old_string,
                    "new": new_string,
                    "replace_all": replace_all,
                },
            )
            with self._lease_backend() as backend, backend._rooted_file_operation():
                response = backend.execute(request.command)
            return self._project_edit_response(
                requested=mapped,
                old_string=old_string,
                response=response,
                request=request,
            )

        old_path, new_path = self._edit_staging_paths()
        request = _build_rooted_command(
            root=self._root,
            operation="edit",
            arguments={
                "path": mapped.virtual,
                "old_path": old_path,
                "new_path": new_path,
                "replace_all": replace_all,
            },
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            try:
                upload_responses = backend.upload_files(
                    [
                        (old_path, old_string.encode("utf-8")),
                        (new_path, new_string.encode("utf-8")),
                    ]
                )
                upload_error = self._edit_upload_error(
                    requested=mapped,
                    responses=upload_responses,
                )
                if upload_error is not None:
                    return upload_error
                response = backend.execute(request.command)
            finally:
                self._cleanup_edit_staging(
                    backend,
                    old_path=old_path,
                    new_path=new_path,
                )
        return self._project_edit_response(
            requested=mapped,
            old_string=old_string,
            response=response,
            request=request,
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Asynchronously edit a text file within the virtual root."""
        mapped = self._map_path(file_path)
        if mapped is None:
            return EditResult(error=self._invalid_path_error(file_path))
        payload_size = len(old_string.encode("utf-8")) + len(new_string.encode("utf-8"))
        if payload_size <= _ROOTED_EDIT_INLINE_MAX_BYTES:
            request = _build_rooted_command(
                root=self._root,
                operation="edit",
                arguments={
                    "path": mapped.virtual,
                    "old": old_string,
                    "new": new_string,
                    "replace_all": replace_all,
                },
            )

            async def inline_operation(backend: OpenSandboxBackend) -> EditResult:
                with backend._rooted_file_operation():
                    response = await backend.aexecute(request.command)
                return self._project_edit_response(
                    requested=mapped,
                    old_string=old_string,
                    response=response,
                    request=request,
                )

            return await self._run_async(inline_operation)

        old_path, new_path = self._edit_staging_paths()
        request = _build_rooted_command(
            root=self._root,
            operation="edit",
            arguments={
                "path": mapped.virtual,
                "old_path": old_path,
                "new_path": new_path,
                "replace_all": replace_all,
            },
        )

        async def operation(backend: OpenSandboxBackend) -> EditResult:
            with backend._rooted_file_operation():
                try:
                    upload_responses = await backend.aupload_files(
                        [
                            (old_path, old_string.encode("utf-8")),
                            (new_path, new_string.encode("utf-8")),
                        ]
                    )
                    upload_error = self._edit_upload_error(
                        requested=mapped,
                        responses=upload_responses,
                    )
                    if upload_error is not None:
                        return upload_error
                    response = await backend.aexecute(request.command)
                finally:
                    await self._acleanup_edit_staging(
                        backend,
                        old_path=old_path,
                        new_path=new_path,
                    )
            return self._project_edit_response(
                requested=mapped,
                old_string=old_string,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a path within the virtual root but never the root itself."""
        mapped = self._map_path(file_path)
        if mapped is None or mapped.virtual == "/":
            return DeleteResult(error=self._invalid_path_error(file_path))
        request = _build_rooted_command(
            root=self._root,
            operation="delete",
            arguments={"path": mapped.virtual},
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_delete_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def adelete(self, file_path: str) -> DeleteResult:
        """Asynchronously delete a path but never the virtual root itself."""
        mapped = self._map_path(file_path)
        if mapped is None or mapped.virtual == "/":
            return DeleteResult(error=self._invalid_path_error(file_path))
        request = _build_rooted_command(
            root=self._root,
            operation="delete",
            arguments={"path": mapped.virtual},
        )

        async def operation(backend: OpenSandboxBackend) -> DeleteResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_delete_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def ls(self, path: str) -> LsResult:
        """List a virtual directory and restore model-visible entry paths."""
        mapped = self._map_path(path)
        if mapped is None:
            return LsResult(error=self._invalid_path_error(path), entries=None)
        request = _build_rooted_command(
            root=self._root,
            operation="list",
            arguments={"path": mapped.virtual},
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_list_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def als(self, path: str) -> LsResult:
        """Asynchronously list a virtual directory and restore entry paths."""
        mapped = self._map_path(path)
        if mapped is None:
            return LsResult(error=self._invalid_path_error(path), entries=None)
        request = _build_rooted_command(
            root=self._root,
            operation="list",
            arguments={"path": mapped.virtual},
        )

        async def operation(backend: OpenSandboxBackend) -> LsResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_list_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Run a glob within a virtual search root and restore matched paths."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or not self._is_safe_path_pattern(pattern):
            return GlobResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="glob",
            arguments={"path": mapped.virtual, "pattern": pattern},
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_glob_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Asynchronously run a glob within a virtual search root."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or not self._is_safe_path_pattern(pattern):
            return GlobResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="glob",
            arguments={"path": mapped.virtual, "pattern": pattern},
        )

        async def operation(backend: OpenSandboxBackend) -> GlobResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_glob_response(
                requested=mapped,
                response=response,
                request=request,
            )

        return await self._run_async(operation)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Search text within a virtual root and restore matched paths."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or (
            glob is not None and not self._is_safe_path_pattern(glob)
        ):
            return GrepResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="grep",
            arguments={
                "path": mapped.virtual,
                "pattern": pattern,
                "glob": glob,
                "max_count": max_count,
            },
        )
        with self._lease_backend() as backend, backend._rooted_file_operation():
            response = backend.execute(request.command)
        return self._project_grep_response(
            requested=mapped,
            response=response,
            request=request,
        )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Asynchronously search text within a virtual root."""
        search_input = path or "/"
        mapped = self._map_path(search_input)
        if mapped is None or (
            glob is not None and not self._is_safe_path_pattern(glob)
        ):
            return GrepResult(
                error=self._invalid_path_error(search_input),
                matches=None,
            )
        request = _build_rooted_command(
            root=self._root,
            operation="grep",
            arguments={
                "path": mapped.virtual,
                "pattern": pattern,
                "glob": glob,
                "max_count": max_count,
            },
        )

        async def operation(backend: OpenSandboxBackend) -> GrepResult:
            with backend._rooted_file_operation():
                response = await backend.aexecute(request.command)
            return self._project_grep_response(
                requested=mapped,
                response=response,
                request=request,
            )

        task, state = self._start_async_task(operation)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=ASYNC_GREP_TIMEOUT,
            )
        except TimeoutError:
            if not state.has_started:
                task.cancel()
            logger.warning(
                "OpenSandbox rooted grep timed out after %s seconds: path=%r glob=%r",
                ASYNC_GREP_TIMEOUT,
                path,
                glob,
            )
            return GrepResult(
                error=(
                    f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. "
                    "Try a more specific pattern or a narrower path."
                )
            )
        except asyncio.CancelledError:
            if not state.has_started:
                task.cancel()
            raise

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Reject synchronous remote capture; use :meth:`aexecute_with_offload`."""
        del command, capture_path, max_inline_bytes, max_capture_bytes, timeout
        self._reject_sync_remote_io()

    async def aexecute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Asynchronously map a capture path or execute without offload."""
        mapped = self._map_path(capture_path)

        async def operation(backend: OpenSandboxBackend) -> ExecuteOffloadResult:
            if mapped is not None:
                return await backend._aexecute_rooted_offload(
                    root=self._root,
                    command=command,
                    capture_path=mapped.virtual,
                    max_inline_bytes=max_inline_bytes,
                    max_capture_bytes=max_capture_bytes,
                    timeout=timeout,
                )
            response = await backend.aexecute(command, timeout=timeout)
            return ExecuteOffloadResult(offloaded=False, response=response)

        return await self._run_async(operation)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Reject valid sync downloads while returning local invalid-only batches."""
        mapped = [self._map_path(path) for path in paths]
        if any(item is not None for item in mapped):
            self._reject_sync_remote_io()
        return [
            FileDownloadResponse(
                path=path,
                content=None,
                error=INVALID_PATH,
            )
            for path in paths
        ]

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Asynchronously download paths mapped through the virtual root."""
        mapped = [self._map_path(path) for path in paths]
        candidates = [item for item in mapped if item is not None]
        if not candidates:
            return [
                FileDownloadResponse(
                    path=path,
                    content=None,
                    error=INVALID_PATH,
                )
                for path in paths
            ]

        async def operation(
            backend: OpenSandboxBackend,
        ) -> list[FileDownloadResponse]:
            responses: list[FileDownloadResponse] = []
            for index, item in enumerate(mapped):
                if item is None:
                    responses.append(
                        FileDownloadResponse(
                            path=paths[index],
                            content=None,
                            error=INVALID_PATH,
                        )
                    )
                    continue
                raw = await backend._adownload_rooted_file(
                    root=self._root,
                    path=item.virtual,
                )
                responses.append(
                    FileDownloadResponse(
                        path=item.requested,
                        content=raw.content,
                        error=self._restore_error(raw.error),
                    )
                )
            return responses

        return await self._run_async(operation)

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Reject valid sync uploads while returning local invalid-only batches."""
        mapped = [self._map_path(path) for path, _ in files]
        if any(item is not None for item in mapped):
            self._reject_sync_remote_io()
        return [FileUploadResponse(path=path, error=INVALID_PATH) for path, _ in files]

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Asynchronously upload paths mapped through the virtual root."""
        mapped = [self._map_path(path) for path, _ in files]
        candidates = [item for item in mapped if item is not None]
        if not candidates:
            return [
                FileUploadResponse(path=path, error=INVALID_PATH) for path, _ in files
            ]

        async def operation(
            backend: OpenSandboxBackend,
        ) -> list[FileUploadResponse]:
            responses: list[FileUploadResponse] = []
            for index, item in enumerate(mapped):
                if item is None:
                    responses.append(
                        FileUploadResponse(
                            path=files[index][0],
                            error=INVALID_PATH,
                        )
                    )
                    continue
                raw = await backend._aupload_rooted_file(
                    root=self._root,
                    path=item.virtual,
                    content=files[index][1],
                )
                responses.append(
                    FileUploadResponse(
                        path=item.requested,
                        error=self._restore_error(raw.error),
                    )
                )
            return responses

        return await self._run_async(operation)
