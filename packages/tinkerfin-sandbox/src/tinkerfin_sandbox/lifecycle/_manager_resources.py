"""Owned Sandbox acquisition, warm capacity, and cleanup supervision."""

from __future__ import annotations

__all__ = [
    "_acquire_backend",
    "_await_claim_release",
    "_check_owned_backend",
    "_cleanup_after_health_check",
    "_cleanup_owned_backend",
    "_cleanup_queue_loop",
    "_close_backend",
    "_close_handle",
    "_close_resources",
    "_create_for_warm_claim",
    "_destroy_for_cleanup_claim",
    "_destroy_remote",
    "_dispose_backend",
    "_drain_cleanup_queue",
    "_fill_warm_pool",
    "_is_backend_healthy",
    "_owner_key",
    "_release_cleanup_claim",
    "_renew_backend",
    "_schedule_replenish",
    "_start_backend_cleanup",
    "_take_local_warm_backend",
    "_track_cleanup_task",
    "_wait_for_cleanup_tasks",
]

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from ..backends.handle import OpenSandboxHandle
from ..backends.rooted import RootedOpenSandboxBackend
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxDestroyError,
    OpenSandboxStateOwnershipError,
)
from .state import (
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxWarmClaim,
)

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")
logger = logging.getLogger("tinkerfin_sandbox.lifecycle.manager")


_ManagedBackend = OpenSandboxHandle | RootedOpenSandboxBackend


_HealthBackend = OpenSandboxBackend | OpenSandboxHandle


_CLEANUP_RETRY_INITIAL_SECONDS = 0.05


_CLEANUP_RETRY_MAX_SECONDS = 5.0


_CLEANUP_IDLE_POLL_SECONDS = 5.0


_OWNER_METADATA_KEY = "tinkerfin.ai/owner"


@dataclass(frozen=True, slots=True)
class _BackendAcquisition:
    """Record whether a candidate already owns its authoritative State binding."""

    backend: OpenSandboxBackend
    committed_binding: OpenSandboxBinding | None
    consumed_warm_slot: bool
    retire_after_commit_ids: tuple[str, ...]


def _owner_key(value: str) -> str:
    """Reject values that cannot form a stable owner resource key."""
    if not isinstance(value, str):
        raise TypeError("key_resolver must return a string")
    if not value.strip():
        raise ValueError("key_resolver must return a non-blank string")
    return value


def _owner_metadata_label(owner_digest: str) -> str:
    """Wrap the stable State digest in a valid OpenSandbox label value."""
    return f"owner.{owner_digest}.id"


async def _create_for_warm_claim(
    self: OpenSandboxManager[KeyT],
    claim: OpenSandboxWarmClaim,
) -> OpenSandboxBackend:
    """Create a remote instance while renewing its warm claim."""
    interval = self._state.lease_renew_interval
    if interval is None:
        return await self._client.create()

    creating = asyncio.create_task(
        self._client.create(),
        name=f"tinkerfin-opensandbox-warm-create:{claim.slot}",
    )

    async def renew() -> None:
        while True:
            await asyncio.sleep(interval)
            if not await self._state.renew_warm(claim):
                raise OpenSandboxStateOwnershipError(
                    f"Warm slot {claim.slot} was lost during remote creation"
                )

    renewing = asyncio.create_task(
        renew(),
        name=f"tinkerfin-opensandbox-warm-lease:{claim.slot}",
    )
    claimed = False
    backend: OpenSandboxBackend | None = None
    primary: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            {creating, renewing},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if renewing in done:
            renewal_error = renewing.exception()
            if renewal_error is not None:
                raise renewal_error
        backend = creating.result()
        claimed = True
    except BaseException as error:  # noqa: BLE001 - settle before propagation
        primary = error

    if not creating.done():
        creating.cancel()
    if not renewing.done():
        renewing.cancel()

    async def settle_children() -> None:
        create_result, _ = await asyncio.gather(
            creating,
            renewing,
            return_exceptions=True,
        )
        if not claimed and not isinstance(create_result, BaseException):
            await self._cleanup_owned_backend(create_result, destroy=True)

    settlement = asyncio.create_task(
        settle_children(),
        name=f"tinkerfin-opensandbox-warm-create-settlement:{claim.slot}",
    )
    self._track_cleanup_task(settlement)
    try:
        await self._await_claim_release(settlement)
    except asyncio.CancelledError as cancellation:
        if isinstance(primary, asyncio.CancelledError):
            primary.add_note(
                "OpenSandbox warm creation settlement also received caller "
                f"cancellation: {cancellation}"
            )
        else:
            if primary is not None:
                cancellation.add_note(
                    "OpenSandbox warm creation also failed: "
                    f"{type(primary).__name__}: {primary}"
                )
            primary = cancellation
    except BaseException as settlement_error:  # noqa: BLE001 - owned settlement
        if primary is None:
            primary = settlement_error
        else:
            primary.add_note(
                "OpenSandbox warm creation settlement also failed: "
                f"{type(settlement_error).__name__}: {settlement_error}"
            )

    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
    assert backend is not None
    return backend


async def _is_backend_healthy(
    self: OpenSandboxManager[KeyT], backend: _HealthBackend
) -> bool:
    """Treat nonzero health results and all probe failures as unhealthy."""
    try:
        response = await backend.aexecute(
            self._client.config.health_command,
        )
        return response.exit_code == 0
    except Exception:
        logger.info("Sandbox %s health check failed", backend.id, exc_info=True)
        return False


async def _renew_backend(
    self: OpenSandboxManager[KeyT], backend: _HealthBackend
) -> None:
    """Best-effort renewal without invalidating an otherwise healthy handle."""
    try:
        await backend.arenew(self._client.config.ttl)
    except Exception:
        logger.warning("Failed to renew Sandbox %s", backend.id, exc_info=True)


async def _close_backend(
    self: OpenSandboxManager[KeyT], backend: OpenSandboxBackend
) -> None:
    """Best-effort local closure that does not mask the primary result."""
    try:
        await backend.aclose()
    except Exception:
        logger.warning(
            "Failed to close local resources for Sandbox %s",
            backend.id,
            exc_info=True,
        )


async def _close_handle(
    self: OpenSandboxManager[KeyT], handle: OpenSandboxHandle
) -> None:
    """Retire a handle and close its local backend through the manager."""
    try:
        await handle._aclose_from_manager()
    except Exception:
        logger.warning("Failed to close Sandbox handle %s", handle.id, exc_info=True)


def _track_cleanup_task(
    self: OpenSandboxManager[KeyT],
    task: asyncio.Task[None],
) -> None:
    """Retain a cleanup task until completion so ``aclose`` can await it."""
    self._cleanup_tasks.add(task)
    task.add_done_callback(self._cleanup_tasks.discard)


def _start_backend_cleanup(
    self: OpenSandboxManager[KeyT],
    backend: OpenSandboxBackend,
    *,
    destroy: bool,
) -> asyncio.Task[None]:
    """Create a managed cleanup task and transfer backend ownership immediately."""
    cleanup = (
        self._dispose_backend(backend) if destroy else self._close_backend(backend)
    )
    task = asyncio.create_task(cleanup)
    self._track_cleanup_task(task)
    return task


async def _cleanup_owned_backend(
    self: OpenSandboxManager[KeyT],
    backend: OpenSandboxBackend,
    *,
    destroy: bool,
) -> None:
    """Shield cleanup so a currently owned backend is always disposed."""
    await asyncio.shield(self._start_backend_cleanup(backend, destroy=destroy))


async def _cleanup_after_health_check(
    self: OpenSandboxManager[KeyT],
    backend: OpenSandboxBackend,
    health_task: asyncio.Task[bool],
    *,
    destroy: bool,
) -> None:
    """Settle a shielded native health probe before disposing its backend."""
    try:
        await health_task
    finally:
        if destroy:
            await self._dispose_backend(backend)
        else:
            await self._close_backend(backend)


async def _check_owned_backend(
    self: OpenSandboxManager[KeyT],
    backend: OpenSandboxBackend,
    *,
    destroy_on_cancel: bool,
) -> bool:
    """Check an unpublished backend while retaining cleanup on cancellation.

    ``destroy_on_cancel`` is true only while no authoritative State binding can
    reference the remote instance. A recovered or consumed-warm binding owns only
    this worker's local connection and must preserve the remote Sandbox.
    """
    health_task = asyncio.create_task(self._is_backend_healthy(backend))
    try:
        return await asyncio.shield(health_task)
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(
            self._cleanup_after_health_check(
                backend,
                health_task,
                destroy=destroy_on_cancel,
            )
        )
        self._track_cleanup_task(cleanup_task)
        raise


async def _wait_for_cleanup_tasks(self: OpenSandboxManager[KeyT]) -> None:
    """Wait for current cleanup tasks and any tasks they derive.

    Removing each completed snapshot explicitly avoids a busy loop when task done
    callbacks have not yet removed those same tasks from the retained set.
    """
    while self._cleanup_tasks:
        tasks = tuple(self._cleanup_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup_tasks.difference_update(tasks)


async def _destroy_remote(
    self: OpenSandboxManager[KeyT],
    sandbox_id: str,
    *,
    strict: bool = False,
) -> None:
    """Choose strict destruction or best-effort reclamation by call site.

    ``delete`` uses strict mode so failed bindings remain retryable. Replacement
    and warm-pool shutdown use best effort so stale cleanup cannot block a new
    handle or process closure.
    """
    try:
        await self._client.destroy(sandbox_id)
    except Exception as exc:
        if strict:
            raise OpenSandboxDestroyError(
                f"Failed to destroy remote Sandbox {sandbox_id!r}; binding retained"
            ) from exc
        try:
            await self._state.enqueue_cleanup(sandbox_id)
        except Exception:
            logger.error(
                "Failed to persist cleanup work for Sandbox %s",
                sandbox_id,
                exc_info=True,
            )
        else:
            self._cleanup_wakeup.set()
        logger.warning("Failed to destroy remote Sandbox %s", sandbox_id, exc_info=True)


async def _drain_cleanup_queue(self: OpenSandboxManager[KeyT]) -> bool:
    """Consume claimable orphan work and report whether retry needs backoff."""
    while True:
        claim = await self._state.claim_cleanup()
        if claim is None:
            return False
        try:
            await self._destroy_for_cleanup_claim(claim)
        except asyncio.CancelledError as cancellation:
            try:
                await self._release_cleanup_claim(claim)
            except asyncio.CancelledError:
                pass
            raise cancellation
        except Exception:
            logger.warning(
                "Failed to clean up orphan Sandbox %s",
                claim.sandbox_id,
                exc_info=True,
            )
            await self._release_cleanup_claim(claim)
            return True
        await self._state.complete_cleanup(claim)


async def _release_cleanup_claim(
    self: OpenSandboxManager[KeyT],
    claim: OpenSandboxCleanupClaim,
) -> None:
    """Settle cleanup-claim release before propagating caller cancellation."""
    release_task = asyncio.create_task(
        self._state.release_cleanup(claim),
        name=f"tinkerfin-opensandbox-cleanup-release:{claim.sandbox_id}",
    )
    await self._await_claim_release(release_task)


async def _await_claim_release(release_task: asyncio.Task[None]) -> None:
    """Settle State release before propagating repeated caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while not release_task.done():
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
    release_task.result()
    if cancellation is not None:
        raise cancellation


async def _destroy_for_cleanup_claim(
    self: OpenSandboxManager[KeyT],
    claim: OpenSandboxCleanupClaim,
) -> None:
    """Destroy an orphan resource while renewing its cleanup claim."""
    interval = self._state.lease_renew_interval
    if interval is None:
        await self._client.destroy(claim.sandbox_id)
        return

    destroying = asyncio.create_task(
        self._client.destroy(claim.sandbox_id),
        name=f"tinkerfin-opensandbox-cleanup-destroy:{claim.sandbox_id}",
    )

    async def renew() -> None:
        while True:
            await asyncio.sleep(interval)
            if not await self._state.renew_cleanup(claim):
                raise OpenSandboxStateOwnershipError(
                    f"Cleanup claim for {claim.sandbox_id!r} was lost "
                    "during remote destruction"
                )

    renewing = asyncio.create_task(
        renew(),
        name=f"tinkerfin-opensandbox-cleanup-lease:{claim.sandbox_id}",
    )
    try:
        done, _ = await asyncio.wait(
            {destroying, renewing},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if renewing in done:
            renewal_error = renewing.exception()
            if renewal_error is not None:
                raise renewal_error
        destroying.result()
    finally:
        if not destroying.done():
            destroying.cancel()
        if not renewing.done():
            renewing.cancel()
        await asyncio.gather(
            destroying,
            renewing,
            return_exceptions=True,
        )


async def _cleanup_queue_loop(
    self: OpenSandboxManager[KeyT], *, retry_pending: bool
) -> None:
    """Process durable cleanup left by this process and other workers."""
    retry_delay = _CLEANUP_RETRY_INITIAL_SECONDS
    while not self._closed:
        if retry_pending:
            await asyncio.sleep(retry_delay)
        else:
            try:
                await asyncio.wait_for(
                    self._cleanup_wakeup.wait(),
                    timeout=_CLEANUP_IDLE_POLL_SECONDS,
                )
            except TimeoutError:
                pass
        self._cleanup_wakeup.clear()
        if self._closed:
            return
        try:
            retry_pending = await self._drain_cleanup_queue()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Failed to consume the OpenSandbox cleanup queue", exc_info=True
            )
            retry_pending = True
        if retry_pending:
            retry_delay = min(
                retry_delay * 2,
                _CLEANUP_RETRY_MAX_SECONDS,
            )
        else:
            retry_delay = _CLEANUP_RETRY_INITIAL_SECONDS


async def _dispose_backend(
    self: OpenSandboxManager[KeyT],
    backend: OpenSandboxBackend,
    *,
    strict: bool = False,
) -> None:
    """Attempt remote destruction before unconditionally closing locally."""
    try:
        await self._destroy_remote(backend.id, strict=strict)
    finally:
        await self._close_backend(backend)


async def _fill_warm_pool(
    self: OpenSandboxManager[KeyT], *, fail_on_error: bool = False
) -> None:
    """Claim and fill global warm slots through State."""
    async with self._warm_fill_lock:
        while True:
            if self._closed:
                return
            claim = await self._state.claim_warm_slot()
            if claim is None:
                return
            backend = None
            published = False
            try:
                backend = await self._create_for_warm_claim(claim)
                await self._state.publish_warm(claim, backend.id)
                published = True
            except asyncio.CancelledError:
                if backend is not None:
                    await self._cleanup_owned_backend(backend, destroy=True)
                raise
            except Exception:
                logger.warning("Failed to create a warm Sandbox", exc_info=True)
                if backend is not None:
                    await self._cleanup_owned_backend(backend, destroy=True)
                if fail_on_error:
                    raise
                return
            finally:
                if not published:
                    release_task = asyncio.create_task(
                        self._state.release_warm(claim),
                        name=(f"tinkerfin-opensandbox-warm-release:{claim.slot}"),
                    )
                    await self._await_claim_release(release_task)

            async with self._warm_lock:
                self._warm_backends.append(backend)


def _schedule_replenish(self: OpenSandboxManager[KeyT]) -> None:
    """Schedule at most one replenishment task for an open undersized pool."""
    if self._closed or not self._started or self._warm_pool_size == 0:
        return
    if self._replenish_task is not None and not self._replenish_task.done():
        return
    self._replenish_task = asyncio.create_task(
        self._fill_warm_pool(fail_on_error=False)
    )


async def _take_local_warm_backend(
    self: OpenSandboxManager[KeyT],
    sandbox_id: str,
) -> OpenSandboxBackend | None:
    """Take the local backend retained for one shared warm slot."""
    async with self._warm_lock:
        for index, backend in enumerate(self._warm_backends):
            if backend.id == sandbox_id:
                return self._warm_backends.pop(index)
    return None


async def _acquire_backend(
    self: OpenSandboxManager[KeyT],
    claim: OpenSandboxOwnerClaim,
) -> _BackendAcquisition:
    """Acquire a healthy candidate backend uniquely owned by the caller.

    State first transfers a global warm slot atomically to the owner. Instances
    created by this process reuse their local backend; instances created by other
    workers reconnect by remote ID. Unhealthy instances are destroyed before the
    manager tries another warm slot or creates on demand.

    Returns:
        Candidate backend, optional committed binding, warm-slot fact, and remote
        IDs that become reclaimable only after this candidate is authoritative.
    """
    consumed_warm_slot = False
    current_binding_id: str | None = None
    retire_after_commit_ids: list[str] = []
    while True:
        binding = await self._state.consume_warm(claim)
        if binding is None:
            try:
                pending_retire_ids = [*retire_after_commit_ids]
                if current_binding_id is not None:
                    pending_retire_ids.append(current_binding_id)
                return _BackendAcquisition(
                    backend=await self._client.create(
                        metadata={
                            _OWNER_METADATA_KEY: _owner_metadata_label(
                                claim.owner_digest
                            )
                        }
                    ),
                    committed_binding=None,
                    consumed_warm_slot=consumed_warm_slot,
                    retire_after_commit_ids=tuple(pending_retire_ids),
                )
            except (Exception, asyncio.CancelledError):
                if consumed_warm_slot:
                    self._schedule_replenish()
                raise

        consumed_warm_slot = True
        if current_binding_id is not None and current_binding_id != binding.sandbox_id:
            retire_after_commit_ids.append(current_binding_id)
        current_binding_id = binding.sandbox_id
        backend = await self._take_local_warm_backend(binding.sandbox_id)
        if backend is None:
            try:
                backend = await self._client.connect(binding.sandbox_id)
            except asyncio.CancelledError:
                self._schedule_replenish()
                raise
            except Exception:
                logger.info(
                    "Failed to reconnect warm Sandbox %s; creating on demand",
                    binding.sandbox_id,
                    exc_info=True,
                )
                continue
        try:
            healthy = await self._check_owned_backend(
                backend,
                destroy_on_cancel=False,
            )
        except asyncio.CancelledError:
            self._schedule_replenish()
            raise
        if healthy:
            return _BackendAcquisition(
                backend=backend,
                committed_binding=binding,
                consumed_warm_slot=True,
                retire_after_commit_ids=tuple(retire_after_commit_ids),
            )
        logger.info("Replacing unhealthy warm Sandbox %s", backend.id)
        try:
            await self._cleanup_owned_backend(backend, destroy=False)
        except asyncio.CancelledError:
            self._schedule_replenish()
            raise


async def _close_resources(self: OpenSandboxManager[KeyT]) -> None:
    """Settle tasks in dependency order before closing pools and connections.

    Startup, public operations, and derived cleanup tasks must finish before pool
    and handle snapshots because they can still add resources. State determines
    whether bound resources are destroyed or retained for another worker.
    """
    start_task = self._start_task
    if start_task is not None:
        try:
            await start_task
        except asyncio.CancelledError:
            logger.info(
                "Sandbox manager startup was cancelled; closing owned resources"
            )
        except Exception:
            logger.warning(
                "Failed while awaiting Sandbox manager startup", exc_info=True
            )

    await self._operations_done.wait()

    cleanup_queue_task = self._cleanup_queue_task
    if cleanup_queue_task is not None:
        if not cleanup_queue_task.done():
            cleanup_queue_task.cancel()
        await asyncio.gather(cleanup_queue_task, return_exceptions=True)
    self._cleanup_queue_task = None

    await self._wait_for_cleanup_tasks()

    replenish_task = self._replenish_task
    if replenish_task is not None:
        try:
            await replenish_task
        except Exception:
            logger.warning(
                "Failed while awaiting warm-pool replenishment", exc_info=True
            )
    self._replenish_task = None

    async with self._warm_lock:
        warm_backends = self._warm_backends
        self._warm_backends = []
    destroyed_ids: set[str] = set()
    for backend in warm_backends:
        if self._state.persistent:
            await self._close_backend(backend)
        else:
            await self._dispose_backend(backend)
            destroyed_ids.add(backend.id)

    handles = list(self._handles.values())
    self._handles.clear()
    self._backend_views.clear()
    for handle in handles:
        if self._state.persistent:
            await self._close_handle(handle)
            continue
        try:
            backend = await handle._aretire()
        except Exception:
            logger.warning(
                "Failed to retire process-local Sandbox handle %s",
                handle.id,
                exc_info=True,
            )
            continue
        await self._dispose_backend(backend)
        destroyed_ids.add(backend.id)

    shutdown_ids: set[str] = set()
    if self._started:
        try:
            shutdown_ids.update(await self._state.shutdown_sandbox_ids())
        except Exception:
            logger.warning("Failed to read State shutdown resources", exc_info=True)
    for pending in self._pending_destroy_ids.values():
        shutdown_ids.update(pending)
    for sandbox_id in shutdown_ids - destroyed_ids:
        await self._destroy_remote(sandbox_id)
    self._pending_destroy_ids.clear()

    try:
        await self._state.aclose()
    except Exception:
        logger.warning("Failed to close OpenSandbox State", exc_info=True)
    try:
        await self._client.aclose()
    except Exception:
        logger.warning("Failed to close OpenSandbox client", exc_info=True)
