"""Native asynchronous lifecycle client for OpenSandbox instances.

The client uses the asynchronous OpenSandbox 0.1.14 API. It retains creation and
connection tasks after caller cancellation so late resources are reclaimed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from opensandbox import Sandbox
from opensandbox import SandboxManager as OpenSandboxSDKManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxReadyTimeoutException
from opensandbox.models import WriteEntry
from opensandbox.models.sandboxes import SandboxFilter, SandboxInfo

from ..backends.sdk import OpenSandboxBackend, unavailable_reason
from ..errors import (
    OpenSandboxBackendError,
    OpenSandboxBackendProtocolError,
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    UnexpectedOpenSandboxBackendError,
)
from ..models import OpenSandboxConfig, OpenSandboxRuntimeInfo
from ._protocols import _SandboxClient

logger = logging.getLogger(__name__)

_CREATE_TOKEN_METADATA_KEY = "tinkerfin.ai/create-token"


def _backend_error(
    operation: str,
    error: Exception,
) -> OpenSandboxBackendError:
    diagnostic_context = {
        "implementation": "opensandbox_sdk",
        "operation": operation,
    }
    if isinstance(error, OpenSandboxBackendError):
        error._enrich_diagnostic_context(diagnostic_context)
        return error
    if isinstance(error, SandboxReadyTimeoutException | TimeoutError):
        return OpenSandboxBackendTimeoutError(
            f"OpenSandbox {operation} timed out",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    if isinstance(error, (TypeError, ValueError)):
        return OpenSandboxBackendProtocolError(
            f"OpenSandbox {operation} returned an invalid response",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    reason = unavailable_reason(error)
    if reason in {"not_found", "unhealthy", "unavailable"} or isinstance(
        error, OSError
    ):
        return OpenSandboxBackendUnavailableError(
            f"OpenSandbox is unavailable for {operation}",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    return UnexpectedOpenSandboxBackendError(
        f"OpenSandbox {operation} failed",
        diagnostic_context=diagnostic_context,
        cause=error,
    )


OpenSandboxInitializer = Callable[
    [OpenSandboxBackend],
    Awaitable[None] | None,
]


class OpenSandboxClient(_SandboxClient):
    """Create, connect, inspect, and destroy native asynchronous Sandboxes.

    ``create`` transfers backend ownership to its caller. ``connect`` opens only a
    local connection, ``inspect`` is read-only, and ``destroy`` is idempotent when
    the remote instance is absent. Initializers run in declaration order and may be
    native async callbacks or non-blocking synchronous callbacks.
    """

    _tinkerfin_error_boundary = True

    def __init__(
        self,
        *,
        connection_config: ConnectionConfig | None,
        config: OpenSandboxConfig | None = None,
        initializers: Sequence[OpenSandboxInitializer] = (),
    ) -> None:
        """Configure the client without opening a connection.

        Args:
            connection_config: SDK endpoint, authentication, and asynchronous
                transport configuration. ``None`` lets the SDK read its standard
                environment variables.
            config: Image, resources, mounts, timeouts, metadata, and health policy.
            initializers: Idempotent callbacks run in order after creation or connect.
        """
        self.config = config or OpenSandboxConfig()
        self.connection_config = self._resolve_connection_config(connection_config)
        self._initializers = tuple(initializers)
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    def _resolve_connection_config(
        self,
        connection_config: ConnectionConfig | None,
    ) -> ConnectionConfig:
        """Replace only the SDK's implicit short timeout for cold image pulls."""
        if connection_config is None:
            return ConnectionConfig(
                request_timeout=self.config.lifecycle_request_timeout
            )
        if "request_timeout" in connection_config.model_fields_set:
            return connection_config
        return connection_config.model_copy(
            update={
                "request_timeout": self.config.lifecycle_request_timeout,
            }
        )

    def _wrap(self, sandbox: Sandbox) -> OpenSandboxBackend:
        """Wrap an SDK sandbox as an asynchronous Deep Agents backend."""
        return OpenSandboxBackend(
            sandbox=sandbox,
            default_timeout=self.config.command_timeout,
            command_env=self.config.command_env,
            working_directory=self.config.workspace_root,
            health_command=self.config.health_command,
            enable_capture_offload=self.config.enable_capture_offload,
        )

    async def _initialize(self, backend: OpenSandboxBackend) -> None:
        """Run initializers in declaration order and stop at the first failure."""
        for initializer in self._initializers:
            result = initializer(backend)
            if inspect.isawaitable(result):
                await result

    async def _initialize_workspace(self, sandbox: Sandbox) -> None:
        """Idempotently create the shell and file-tool workspace through the SDK."""
        workspace_root = self.config.workspace_root
        if workspace_root is None:
            return
        await sandbox.files.create_directories(
            # SDK 0.1.14 expects decimal Unix permission text, not 0o755.
            [WriteEntry(path=workspace_root, mode=755)]
        )

    @staticmethod
    async def _close_quietly(
        backend: OpenSandboxBackend,
        *,
        operation: str,
    ) -> None:
        try:
            await backend.aclose()
        except Exception:
            logger.warning(
                "Failed to close local resources for sandbox %s after %s",
                backend.id,
                operation,
                exc_info=True,
            )

    @staticmethod
    async def _close_sdk_quietly(
        sandbox: Sandbox,
        *,
        sandbox_id: str,
        operation: str,
    ) -> None:
        try:
            await sandbox.close()
        except Exception:
            logger.warning(
                "Failed to close local SDK resources for sandbox %s after %s",
                sandbox_id,
                operation,
                exc_info=True,
            )

    async def _create(
        self,
        metadata: Mapping[str, str] | None,
    ) -> OpenSandboxBackend:
        """Create and initialize a sandbox, reclaiming it on initialization failure."""
        volumes = [volume.model_copy(deep=True) for volume in self.config.volumes]
        creation_metadata = dict(self.config.metadata)
        creation_metadata.update(metadata or {})
        creation_metadata[_CREATE_TOKEN_METADATA_KEY] = uuid4().hex
        try:
            sandbox = await Sandbox.create(
                self.config.image,
                entrypoint=list(self.config.entrypoint),
                env=dict(self.config.env),
                metadata=creation_metadata,
                resource=dict(self.config.resource),
                volumes=volumes or None,
                timeout=self.config.ttl,
                ready_timeout=self.config.ready_timeout,
                connection_config=self.connection_config,
            )
        except Exception as error:
            try:
                sandbox = await self._recover_unknown_create(creation_metadata)
            except Exception as recovery_error:  # noqa: BLE001 - SDK recovery boundary
                error.add_note(
                    "OpenSandbox create recovery also failed: "
                    f"{type(recovery_error).__name__}"
                )
                translated = _backend_error("create", error)
                raise translated from error
            if sandbox is None:
                translated = _backend_error("create", error)
                raise translated from error
        backend = self._wrap(sandbox)
        try:
            await self._initialize_workspace(sandbox)
        except Exception as error:
            try:
                await backend.akill()
            except Exception:
                logger.warning(
                    "Failed to reclaim newly created sandbox %s after initialization",
                    backend.id,
                    exc_info=True,
                )
            await self._close_quietly(backend, operation="initialization failure")
            translated = _backend_error("workspace initialization", error)
            raise translated from error
        try:
            await self._initialize(backend)
        except BaseException:
            try:
                await backend.akill()
            except Exception:
                logger.warning(
                    "Failed to reclaim newly created sandbox %s after initialization",
                    backend.id,
                    exc_info=True,
                )
            await self._close_quietly(backend, operation="initializer failure")
            raise
        return backend

    async def _recover_unknown_create(
        self,
        creation_metadata: Mapping[str, str],
    ) -> Sandbox | None:
        """Recover a possibly created instance through its unique creation token."""
        token = creation_metadata[_CREATE_TOKEN_METADATA_KEY]
        manager: OpenSandboxSDKManager | None = None
        try:
            manager = await OpenSandboxSDKManager.create(
                connection_config=self.connection_config
            )
            candidates_by_id: dict[str, SandboxInfo] = {}
            page_number = 1
            while True:
                page = await manager.list_sandbox_infos(
                    SandboxFilter(
                        metadata={_CREATE_TOKEN_METADATA_KEY: token},
                        page_size=2,
                        page=page_number,
                    )
                )
                for info in page.sandbox_infos:
                    if (
                        info.metadata is not None
                        and info.metadata.get(_CREATE_TOKEN_METADATA_KEY) == token
                    ):
                        candidates_by_id.setdefault(info.id, info)
                if not page.pagination.has_next_page:
                    break
                page_number += 1
            candidates = list(candidates_by_id.values())
            if len(candidates) == 1:
                candidate = candidates[0]
                try:
                    return await Sandbox.connect(
                        candidate.id,
                        connection_config=self.connection_config,
                        connect_timeout=self.config.connect_timeout,
                    )
                except Exception:
                    logger.warning(
                        "Failed to reconnect to sandbox %s with unknown creation result",
                        candidate.id,
                        exc_info=True,
                    )
                    await self._kill_discovered_candidates(manager, candidates)
                    return None
            if len(candidates) > 1:
                logger.error(
                    "Creation token %s matched multiple OpenSandbox instances",
                    token,
                )
                await self._kill_discovered_candidates(manager, candidates)
            return None
        except Exception:
            logger.warning(
                "Failed to query OpenSandbox after an unknown creation result",
                exc_info=True,
            )
            return None
        finally:
            if manager is not None:
                try:
                    await manager.close()
                # Cleanup of a temporary query must not mask the creation outcome.
                except Exception:
                    logger.warning(
                        "Failed to close the creation-token query client",
                        exc_info=True,
                    )

    @staticmethod
    async def _kill_discovered_candidates(
        manager: OpenSandboxSDKManager,
        candidates: Sequence[SandboxInfo],
    ) -> None:
        """Best-effort delete creation-token candidates that cannot be adopted."""
        for candidate in candidates:
            try:
                await manager.kill_sandbox(candidate.id)
            except Exception:
                logger.warning(
                    "Failed to remove creation-token candidate sandbox %s",
                    candidate.id,
                    exc_info=True,
                )

    async def _reclaim_backend(self, backend: OpenSandboxBackend) -> None:
        """Reclaim a remote instance whose creation completed after cancellation."""
        try:
            await backend.akill()
        except Exception:
            logger.warning(
                "Failed to reclaim sandbox %s after creation was cancelled",
                backend.id,
                exc_info=True,
            )
        finally:
            await self._close_quietly(backend, operation="cancelled creation")

    async def _reclaim_cancelled_create(
        self,
        creation_task: asyncio.Task[OpenSandboxBackend],
    ) -> None:
        """Await a shielded creation and take ownership of any returned backend."""
        try:
            backend = await creation_task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            # The original creation path cleans up SDK and initializer failures.
            return
        await self._reclaim_backend(backend)

    async def _close_cancelled_connect(
        self,
        connection_task: asyncio.Task[OpenSandboxBackend],
    ) -> None:
        """Close a late reconnect without destroying the existing remote instance."""
        try:
            backend = await connection_task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            # Failed SDK and initializer paths already close their own connection.
            return
        await self._close_quietly(backend, operation="cancelled reconnect")

    def _track_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Retain a background cleanup task until completion."""
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend:
        """Create a Sandbox, attach metadata, and run every initializer.

        Caller cancellation stops waiting but does not abandon a possibly created
        remote instance. Internal cleanup takes ownership of any late result. A
        unique reserved create token permits discovery after an unconfirmed SDK
        response; ambiguous candidates are destroyed instead of being adopted.

        Args:
            metadata: Additional internal metadata for this creation. Keys are merged
                over host configuration before the reserved create token is added.

        Returns:
            An initialized asynchronous backend owned by the caller.
        """
        creation_task = asyncio.create_task(self._create(metadata))
        try:
            return await asyncio.shield(creation_task)
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(
                self._reclaim_cancelled_create(creation_task)
            )
            self._track_cleanup_task(cleanup_task)
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Repeated cancellation affects only the waiter; cleanup stays retained.
                pass
            raise cancellation

    async def _connect(self, sandbox_id: str) -> OpenSandboxBackend:
        """Strictly reconnect to and initialize an existing remote sandbox."""
        try:
            sandbox = await Sandbox.connect(
                sandbox_id,
                connection_config=self.connection_config,
                connect_timeout=self.config.connect_timeout,
            )
        except Exception as error:
            translated = _backend_error("connect", error)
            raise translated from error
        backend = self._wrap(sandbox)
        try:
            await self._initialize_workspace(sandbox)
        except Exception as error:
            await self._close_quietly(
                backend,
                operation="reconnect initialization failure",
            )
            translated = _backend_error("workspace initialization", error)
            raise translated from error
        try:
            await self._initialize(backend)
        except BaseException:
            await self._close_quietly(
                backend,
                operation="reconnect initializer failure",
            )
            raise
        return backend

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend:
        """Connect to an existing Sandbox without creating a replacement."""
        connection_task = asyncio.create_task(self._connect(sandbox_id))
        try:
            return await asyncio.shield(connection_task)
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(
                self._close_cancelled_connect(connection_task)
            )
            self._track_cleanup_task(cleanup_task)
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Repeated cancellation affects only the waiter; cleanup stays retained.
                pass
            raise cancellation

    async def inspect(self, sandbox_id: str) -> OpenSandboxRuntimeInfo:
        """Read remote details without initialization or lifecycle mutation."""
        sandbox: Sandbox | None = None
        try:
            sandbox = await Sandbox.connect(
                sandbox_id,
                connection_config=self.connection_config,
                connect_timeout=self.config.connect_timeout,
                skip_health_check=True,
            )
            backend = self._wrap(sandbox)
            return await backend.aget_runtime_info()
        except Exception as exc:
            logger.info(
                "Read-only inspection failed for sandbox %s", sandbox_id, exc_info=True
            )
            return OpenSandboxRuntimeInfo.unavailable(
                sandbox_id,
                unavailable_reason(exc),
            )
        finally:
            if sandbox is not None:
                await self._close_sdk_quietly(
                    sandbox,
                    sandbox_id=sandbox_id,
                    operation="read-only inspection",
                )

    async def destroy(self, sandbox_id: str) -> None:
        """Idempotently destroy one remote Sandbox by ID."""
        try:
            sandbox = await Sandbox.connect(
                sandbox_id,
                connection_config=self.connection_config,
                connect_timeout=self.config.connect_timeout,
                skip_health_check=True,
            )
        except Exception as exc:
            if unavailable_reason(exc) == "not_found":
                return
            translated = _backend_error("destroy lookup", exc)
            raise translated from exc
        try:
            await sandbox.kill()
        except Exception as error:
            await self._close_sdk_quietly(
                sandbox,
                sandbox_id=sandbox_id,
                operation="failed destruction",
            )
            translated = _backend_error("destroy", error)
            raise translated from error
        else:
            await sandbox.close()

    async def aclose(self) -> None:
        """Finish cancellation cleanup and close the owned asynchronous transport."""
        while self._cleanup_tasks:
            tasks = tuple(self._cleanup_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.connection_config.close_transport_if_owned()
        except Exception as error:
            translated = _backend_error("client close", error)
            raise translated from error
