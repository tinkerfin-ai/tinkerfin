"""Authoritative Sandbox binding, replacement, and deletion operations."""

from __future__ import annotations

__all__ = [
    "_backend_view",
    "_bind_on_demand_backend",
    "_close_replaced_backend",
    "_delete_locked",
    "_get_locked",
    "_reconcile_candidate_binding",
    "_replace",
    "_retire_replaced_backend",
    "_workspace_root",
]

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypeVar, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware

from ..backends.handle import OpenSandboxHandle
from ..backends.rooted import RootedOpenSandboxBackend
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxDestroyError,
    OpenSandboxResetError,
    OpenSandboxStateError,
)
from ..middleware.filesystem import build_rooted_filesystem_middleware
from ..models import OpenSandboxDetails, _normalize_workspace_root
from ._manager_resources import _ManagedBackend
from .state import OpenSandboxBinding, OpenSandboxOwnerClaim

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")
_BindingResolution: TypeAlias = Literal[
    "authoritative",
    "not_authoritative",
    "unknown",
]


async def _reconcile_candidate_binding(
    self: OpenSandboxManager[KeyT],
    *,
    owner_key: str,
    expected: OpenSandboxBinding,
    primary_error: BaseException,
) -> _BindingResolution:
    """Classify a failed bind from one authoritative State read."""

    try:
        binding = await self._state.read_binding(owner_key)
    except (Exception, asyncio.CancelledError) as reconciliation_error:  # noqa: BLE001 - host State boundary
        primary_error.add_note(
            "OpenSandbox binding reconciliation also failed: "
            f"{type(reconciliation_error).__name__}: {reconciliation_error}"
        )
        return "unknown"
    if binding == expected:
        return "authoritative"
    return "not_authoritative"


async def _bind_on_demand_backend(
    self: OpenSandboxManager[KeyT],
    *,
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
    backend: OpenSandboxBackend,
) -> OpenSandboxBinding:
    """Commit or reconcile one candidate before deciding its cleanup ownership."""

    expected = OpenSandboxBinding(
        sandbox_id=backend.id,
        generation=claim.generation,
    )
    try:
        committed = await self._state.bind_owner(claim, backend.id)
    except asyncio.CancelledError as cancellation:
        resolution = await self._reconcile_candidate_binding(
            owner_key=owner_key,
            expected=expected,
            primary_error=cancellation,
        )
        await self._cleanup_owned_backend(
            backend,
            destroy=resolution == "not_authoritative",
        )
        raise
    except Exception as bind_error:
        resolution = await self._reconcile_candidate_binding(
            owner_key=owner_key,
            expected=expected,
            primary_error=bind_error,
        )
        if resolution == "authoritative":
            return expected
        await self._cleanup_owned_backend(
            backend,
            destroy=resolution == "not_authoritative",
        )
        raise

    if committed != expected:
        mismatch = OpenSandboxStateError(
            "OpenSandbox State returned a binding that does not match the "
            "committed candidate"
        )
        resolution = await self._reconcile_candidate_binding(
            owner_key=owner_key,
            expected=expected,
            primary_error=mismatch,
        )
        if resolution == "authoritative":
            return expected
        await self._cleanup_owned_backend(
            backend,
            destroy=resolution == "not_authoritative",
        )
        raise mismatch
    return committed


async def _replace(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
    existing_handle: OpenSandboxHandle | None,
    *,
    old_id: str | None,
) -> OpenSandboxHandle:
    """Commit a new binding and backend before safely reclaiming the old instance.

    External binding commits before in-memory publication so crash recovery cannot
    restore an old ID. A consumed warm binding is already authoritative; an
    uncertain on-demand bind is reconciled before its local connection is closed or
    its remote Sandbox is destroyed. After publication, stable handle identity is
    preserved while old leases drain; cancellation retains cleanup.
    """
    acquisition = await self._acquire_backend(claim)
    backend = acquisition.backend
    try:
        committed = acquisition.committed_binding
        if committed is None:
            await self._bind_on_demand_backend(
                owner_key=owner_key,
                claim=claim,
                backend=backend,
            )
        elif committed != OpenSandboxBinding(
            sandbox_id=backend.id,
            generation=claim.generation,
        ):
            await self._cleanup_owned_backend(backend, destroy=False)
            raise OpenSandboxStateError(
                "OpenSandbox State returned an incompatible committed warm binding"
            )
    finally:
        if acquisition.consumed_warm_slot:
            self._schedule_replenish()

    old_backend = None
    if existing_handle is None or existing_handle.is_closed:
        handle = OpenSandboxHandle(backend)
    else:
        try:
            old_backend = existing_handle._replace_backend(backend)
        except RuntimeError:
            # Closure can make a handle non-replaceable during lifecycle settlement.
            handle = OpenSandboxHandle(backend)
        else:
            handle = existing_handle
    self._handles[owner_key] = handle

    cleanup_tasks: list[asyncio.Task[None]] = []
    retire_ids = set(acquisition.retire_after_commit_ids)
    retire_ids.discard(backend.id)
    if old_backend is not None:
        cleanup_tasks.append(
            asyncio.create_task(self._retire_replaced_backend(handle, old_backend))
        )
        retire_ids.discard(old_backend.id)
    if old_id is not None and old_id not in {
        backend.id,
        None if old_backend is None else old_backend.id,
    }:
        retire_ids.add(old_id)
    for sandbox_id in sorted(retire_ids):
        cleanup_tasks.append(asyncio.create_task(self._destroy_remote(sandbox_id)))

    # Transfer cleanup ownership for every stale resource before awaiting any one
    # task so cancellation cannot orphan the remainder.
    for cleanup_task in cleanup_tasks:
        self._track_cleanup_task(cleanup_task)
    for cleanup_task in cleanup_tasks:
        await asyncio.shield(cleanup_task)

    await self._renew_backend(handle)
    return handle


async def _retire_replaced_backend(
    self: OpenSandboxManager[KeyT],
    handle: OpenSandboxHandle,
    backend: OpenSandboxBackend,
) -> None:
    """Destroy and close an old backend after its in-flight calls exit."""
    await handle._await_until_idle(backend)
    await self._dispose_backend(backend)


def _workspace_root(self: OpenSandboxManager[KeyT]) -> str | None:
    """Read and validate the client-declared model-visible workspace root."""
    value = getattr(self._client.config, "workspace_root", None)
    return _normalize_workspace_root(value)


def _backend_view(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    handle: OpenSandboxHandle,
) -> _ManagedBackend:
    """Cache a borrowed rooted view with stable per-owner object identity."""
    workspace_root = self._workspace_root()
    if workspace_root is None:
        self._backend_views.pop(owner_key, None)
        return handle
    cached = self._backend_views.get(owner_key)
    if cached is not None and cached[0] is handle:
        return cached[1]
    backend = RootedOpenSandboxBackend(
        handle,
        root=workspace_root,
    )
    self._backend_views[owner_key] = (handle, backend)
    return backend


def build_agent_middleware(
    self: OpenSandboxManager[KeyT],
    backend: BackendProtocol,
    *,
    permissions: Sequence[FilesystemPermission] | None = None,
) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
    """Align Deep Agents file and Shell behavior with the rooted workspace.

    The host supplies the final composed backend so replacement filesystem
    middleware preserves every route it already exposes. No middleware is needed
    when ``workspace_root`` is disabled and raw Sandbox paths are used. Supply the
    same permission rules to ``create_deep_agent`` so interrupt-mode rules install
    the required human-in-the-loop middleware.

    Args:
        backend: Final backend used to construct the Deep Agents graph.
        permissions: Route-scoped filesystem rules enforced by the replacement
            middleware. Rules for executable default backend paths are unsupported
            because Shell commands can bypass file-tool enforcement.

    Returns:
        Fresh OpenSandbox filesystem middleware, or an empty tuple when rooted
        workspaces are disabled.

    Raises:
        NotImplementedError: The backend supports Shell execution and a permission
            path is not scoped to a non-Shell ``CompositeBackend`` route.
    """
    if self._workspace_root() is None:
        return ()
    middleware = build_rooted_filesystem_middleware(
        backend,
        permissions=permissions,
    )
    return (cast(AgentMiddleware[Any, Any, Any], middleware),)


async def get(self: OpenSandboxManager[KeyT], key: KeyT) -> _ManagedBackend:
    """Return the healthy stable backend for one caller-defined key.

    Resolution checks the local handle, committed State binding, warm pool, and
    on-demand creation in that order. An unavailable binding is replaced. Calls
    resolving to the same owner receive the same stable handle.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.

    Returns:
        A manager-owned backend view whose remote Sandbox can be replaced.

    Raises:
        OpenSandboxManagerClosedError: The manager has begun closing.
        OpenSandboxStateError: State acquisition, renewal, or commit failed.
        Exception: The OpenSandbox client could not create a remote instance.
    """
    owner_key = self._resolve_owner_key(key)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            handle = await self._get_locked(owner_key, claim)
            return self._backend_view(owner_key, handle)


async def _get_locked(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
) -> OpenSandboxHandle:
    """Resolve the authoritative binding while holding its State owner claim."""
    self._ensure_open()
    handle = self._handles.get(owner_key)
    stored_id = claim.binding.sandbox_id if claim.binding is not None else None

    if handle is not None and stored_id == handle.id:
        if handle.is_closed:
            old_id = handle.id
            self._handles.pop(owner_key, None)
            return await self._replace(
                owner_key,
                claim,
                None,
                old_id=old_id,
            )
        if await self._is_backend_healthy(handle):
            await self._renew_backend(handle)
            return handle
        return await self._replace(
            owner_key,
            claim,
            handle,
            old_id=handle.id,
        )

    if stored_id is not None:
        try:
            backend = await self._client.connect(stored_id)
        except Exception:  # noqa: BLE001 - reconnect failure triggers replacement
            pass
        else:
            if await self._check_owned_backend(
                backend,
                destroy_on_cancel=False,
            ):
                if handle is not None and not handle.is_closed:
                    old_backend = handle._replace_backend(backend)
                    cleanup_task = asyncio.create_task(
                        self._close_replaced_backend(handle, old_backend)
                    )
                    self._track_cleanup_task(cleanup_task)
                    await asyncio.shield(cleanup_task)
                else:
                    handle = OpenSandboxHandle(backend)
                self._handles[owner_key] = handle
                await self._renew_backend(handle)
                return handle
            await self._cleanup_owned_backend(backend, destroy=False)
        replaceable_handle = (
            handle if handle is not None and not handle.is_closed else None
        )
        return await self._replace(
            owner_key,
            claim,
            replaceable_handle,
            old_id=stored_id,
        )

    replaceable_handle = handle if handle is not None and not handle.is_closed else None
    return await self._replace(
        owner_key,
        claim,
        replaceable_handle,
        old_id=handle.id if handle is not None else None,
    )


async def _close_replaced_backend(
    self: OpenSandboxManager[KeyT],
    handle: OpenSandboxHandle,
    backend: OpenSandboxBackend,
) -> None:
    """Close an idle old connection without destroying its rebound remote instance."""
    await handle._await_until_idle(backend)
    await self._close_backend(backend)


async def recreate(self: OpenSandboxManager[KeyT], key: KeyT) -> _ManagedBackend:
    """Create and commit a replacement Sandbox for one caller-defined key.

    An open handle is updated in place. The previous remote instance is retired
    only after the replacement binding is committed.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.

    Returns:
        The stable backend view pointing at the replacement instance.
    """
    owner_key = self._resolve_owner_key(key)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            self._ensure_open()
            handle = self._handles.get(owner_key)
            old_id = (
                claim.binding.sandbox_id
                if claim.binding is not None
                else handle.id
                if handle is not None
                else None
            )
            replaceable_handle = (
                handle if handle is not None and not handle.is_closed else None
            )
            if handle is not None and handle.is_closed:
                self._handles.pop(owner_key, None)
            replaced = await self._replace(
                owner_key,
                claim,
                replaceable_handle,
                old_id=old_id,
            )
            return self._backend_view(owner_key, replaced)


async def reset(self: OpenSandboxManager[KeyT], key: KeyT) -> None:
    """Clear the configured workspace while retaining identity and binding.

    ``workspace_root`` is the only permitted deletion boundary. Reset is refused
    when it is disabled. Once deletion begins, cancellation waits for the fixed
    workspace operation to settle before releasing the owner claim.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.

    Raises:
        OpenSandboxResetError: No safe workspace is configured or cleanup fails.
    """
    owner_key = self._resolve_owner_key(key)
    workspace_root = self._workspace_root()
    if workspace_root is None:
        raise OpenSandboxResetError(
            "OpenSandbox workspace_root is required for a safe reset"
        )

    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            handle = await self._get_locked(owner_key, claim)
            reset_task = asyncio.create_task(
                handle._areset_workspace_from_manager(workspace_root)
            )
            try:
                await asyncio.shield(reset_task)
            except asyncio.CancelledError as cancellation:
                while not reset_task.done():
                    try:
                        await asyncio.shield(reset_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001
                        break
                try:
                    reset_task.result()
                except Exception:  # noqa: BLE001 - cancellation remains primary
                    pass
                raise cancellation
            except Exception as exc:
                raise OpenSandboxResetError(
                    f"Failed to clear the OpenSandbox workspace for {owner_key!r}"
                ) from exc


async def is_healthy(self: OpenSandboxManager[KeyT], key: KeyT) -> bool:
    """Check only the currently cached local handle for one key.

    This method does not read State, create, reconnect, or renew a Sandbox.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.

    Returns:
        Whether the open cached handle passes its health command.
    """
    owner_key = self._resolve_owner_key(key)
    async with self._operation():
        handle = self._handles.get(owner_key)
        return (
            handle is not None
            and not handle.is_closed
            and await self._is_backend_healthy(handle)
        )


async def get_details(
    self: OpenSandboxManager[KeyT], key: KeyT
) -> OpenSandboxDetails | None:
    """Read stable details for the Sandbox committed to one key.

    This method does not create, renew, reconnect a business handle, or mutate
    State. It may run the configured health command through either the cached
    backend or a temporary read-only connection.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.

    Returns:
        Owner-aware details, or ``None`` when no binding exists.

    Raises:
        OpenSandboxStateError: The authoritative binding could not be read.
    """
    owner_key = self._resolve_owner_key(key)
    async with self._operation():
        binding = await self._state.read_binding(owner_key)
        if binding is None:
            return None
        handle = self._handles.get(owner_key)
        if handle is not None and handle.id == binding.sandbox_id:
            if handle.is_closed:
                runtime = await self._client.inspect(handle.id)
                cached = False
            else:
                runtime = await handle.aget_runtime_info()
                cached = True
            return OpenSandboxDetails.from_runtime(
                runtime,
                owner_key=owner_key,
                cached=cached,
            )
        runtime = await self._client.inspect(binding.sandbox_id)
        return OpenSandboxDetails.from_runtime(
            runtime,
            owner_key=owner_key,
            cached=False,
        )


async def _delete_locked(
    self: OpenSandboxManager[KeyT],
    owner_key: str,
    claim: OpenSandboxOwnerClaim,
) -> None:
    """Run retryable deletion and remove the binding after confirmed destruction.

    Every ID that may belong to the owner enters ``remaining_ids`` first. Strict
    destruction failures retain unconfirmed IDs in memory so a manager without an
    external store can retry deletion.
    """
    handle = self._handles.get(owner_key)
    stored_id = claim.binding.sandbox_id if claim.binding is not None else None
    self._handles.pop(owner_key, None)
    self._backend_views.pop(owner_key, None)

    remaining_ids = set(self._pending_destroy_ids.get(owner_key, ()))
    if stored_id is not None:
        remaining_ids.add(stored_id)
    backend = None
    if handle is not None:
        backend = await handle._aretire()
        remaining_ids.add(backend.id)

    try:
        if backend is not None:
            await self._dispose_backend(backend, strict=True)
            remaining_ids.discard(backend.id)
        for sandbox_id in tuple(remaining_ids):
            await self._destroy_remote(sandbox_id, strict=True)
            remaining_ids.discard(sandbox_id)
    except OpenSandboxDestroyError:
        self._pending_destroy_ids[owner_key] = remaining_ids
        raise

    self._pending_destroy_ids.pop(owner_key, None)
    await self._state.unbind_owner(claim)


async def delete(self: OpenSandboxManager[KeyT], key: KeyT) -> None:
    """Destroy all known instances and remove one key binding.

    Once destruction starts, caller cancellation waits for the internal operation
    to settle. A failed destruction retains the binding or in-memory retry target.

    Args:
        key: Opaque application identity accepted by ``key_resolver``.

    Raises:
        OpenSandboxDestroyError: Destruction is unconfirmed and can be retried.
        OpenSandboxStateError: The State claim or binding mutation failed.
        OpenSandboxManagerClosedError: The manager has begun closing.
    """
    owner_key = self._resolve_owner_key(key)
    async with self._operation():
        async with self._claim_owner(owner_key) as claim:
            self._ensure_open()
            delete_task = asyncio.create_task(self._delete_locked(owner_key, claim))
            try:
                await asyncio.shield(delete_task)
            except asyncio.CancelledError as cancellation:
                # Hold the owner claim until destructive work settles so
                # cancellation cannot lose target IDs.
                while not delete_task.done():
                    try:
                        await asyncio.shield(delete_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001
                        break
                try:
                    delete_task.result()
                except Exception:  # noqa: BLE001 - cancellation remains primary
                    pass
                raise cancellation
