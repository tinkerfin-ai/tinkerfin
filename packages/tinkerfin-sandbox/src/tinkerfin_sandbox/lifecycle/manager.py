"""Manage stable OpenSandbox backends, warm capacity, and remote cleanup.

The manager owns every local connection it opens. Its State determines remote
shutdown behavior: process-local resources are destroyed, while persistent bindings,
shared warm slots, and cleanup work remain available to another worker.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Generic, Literal, Self, TypeVar, cast

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware

from ..backends.handle import OpenSandboxHandle
from ..backends.rooted import RootedOpenSandboxBackend
from ..backends.sdk import OpenSandboxBackend
from ..errors import (
    OpenSandboxDestroyError,
    OpenSandboxManagerClosedError,
    OpenSandboxResetError,
    OpenSandboxSettlementTimeoutError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
)
from ..middleware.filesystem import build_rooted_filesystem_middleware
from ..models import OpenSandboxDetails, _normalize_workspace_root
from ._protocols import _SandboxClient
from .state import (
    InMemoryOpenSandboxState,
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxState,
    OpenSandboxWarmClaim,
)

logger = logging.getLogger(__name__)

_ManagedBackend = OpenSandboxHandle | RootedOpenSandboxBackend
_HealthBackend = OpenSandboxBackend | OpenSandboxHandle
_CLEANUP_RETRY_INITIAL_SECONDS = 0.05
_CLEANUP_RETRY_MAX_SECONDS = 5.0
_CLEANUP_IDLE_POLL_SECONDS = 5.0
_OWNER_METADATA_KEY = "tinkerfin.ai/owner"

KeyT = TypeVar("KeyT")
_BindingResolution = Literal["authoritative", "not_authoritative", "unknown"]


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
    return f"v1.{owner_digest}.v1"


class OpenSandboxManager(Generic[KeyT]):
    """Manage stable OpenSandbox backends for caller-defined ownership keys.

    The State serializes transitions for one owner and fences stale workers while
    allowing unrelated owners to proceed concurrently. A cached
    ``OpenSandboxHandle`` retains its object identity when its remote backend is
    replaced, so existing callers continue using the current Sandbox.

    Cancellation cannot abandon a Sandbox after it leaves the warm pool or client.
    Explicit destruction is strict and retryable; cleanup after a successful
    replacement is durable when the configured State is persistent.
    """

    def __init__(
        self,
        *,
        client: _SandboxClient,
        key_resolver: Callable[[KeyT], str],
        state: OpenSandboxState | None = None,
        warm_pool_size: int | None = None,
        fail_on_startup_warmup_error: bool = False,
        settlement_timeout: float | None = None,
    ) -> None:
        """Configure the manager without opening resources or creating a Sandbox.

        Args:
            client: Asynchronous creation and lifecycle client owned and closed by
                this manager.
            key_resolver: Convert an opaque application key into a stable non-blank
                owner key used by lifecycle State.
            state: Atomic allocation, warm-pool, and cleanup state owned and closed
                by this manager. Omit it to use process-local memory.
            warm_pool_size: Target number of unbound ready Sandboxes. ``None`` uses
                the client configuration.
            fail_on_startup_warmup_error: Whether one failed startup warm creation
                prevents the manager from opening.
            settlement_timeout: Optional per-caller close wait in seconds. ``None``
                waits for complete cleanup. A finite timeout never cancels the shared
                close task.

        Raises:
            TypeError: ``settlement_timeout`` is not numeric or is a boolean.
            ValueError: A capacity is negative, or the timeout is negative or non-finite.
        """
        resolved_warm_size = (
            client.config.warm_pool_size if warm_pool_size is None else warm_pool_size
        )
        if resolved_warm_size < 0:
            raise ValueError("warm_pool_size must not be negative")
        if not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        if settlement_timeout is None:
            resolved_timeout = None
        else:
            if isinstance(settlement_timeout, bool) or not isinstance(
                settlement_timeout,
                int | float,
            ):
                raise TypeError("settlement_timeout must be a number or None")
            resolved_timeout = float(settlement_timeout)
            if not math.isfinite(resolved_timeout) or resolved_timeout < 0:
                raise ValueError("settlement_timeout must be finite and non-negative")

        self._client = client
        self._key_resolver = key_resolver
        self._state = state or InMemoryOpenSandboxState()
        self._warm_pool_size = resolved_warm_size
        self._fail_on_startup_warmup_error = fail_on_startup_warmup_error
        self._settlement_timeout = resolved_timeout
        self._handles: dict[str, OpenSandboxHandle] = {}
        self._backend_views: dict[str, tuple[OpenSandboxHandle, _ManagedBackend]] = {}
        self._warm_backends: list[OpenSandboxBackend] = []
        self._warm_lock = asyncio.Lock()
        self._warm_fill_lock = asyncio.Lock()
        self._replenish_task: asyncio.Task[None] | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_wakeup = asyncio.Event()
        self._cleanup_queue_task: asyncio.Task[None] | None = None
        self._pending_destroy_ids: dict[str, set[str]] = {}
        self._state_lock = asyncio.Lock()
        self._active_operations = 0
        self._operations_done = asyncio.Event()
        self._operations_done.set()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    def _resolve_owner_key(self, key: KeyT) -> str:
        """Resolve one opaque application key at the manager boundary."""
        return _owner_key(self._key_resolver(key))

    async def __aenter__(self) -> Self:
        """Open the State and warm pool, then return this owned resource."""
        try:
            await self.start()
        except BaseException as startup_error:  # noqa: BLE001 - preserve startup outcome
            try:
                await self.aclose()
            except BaseException as cleanup_error:
                current = asyncio.current_task()
                if isinstance(cleanup_error, asyncio.CancelledError) and (
                    current is not None and current.cancelling()
                ):
                    cleanup_error.add_note(
                        "OpenSandbox startup also failed: "
                        f"{type(startup_error).__name__}: {startup_error}"
                    )
                    raise
                startup_error.add_note(
                    "OpenSandbox startup cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise startup_error.with_traceback(startup_error.__traceback__)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close all resources according to the configured State's durability."""
        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            current = asyncio.current_task()
            if isinstance(cleanup_error, asyncio.CancelledError) and (
                current is not None and current.cancelling()
            ):
                cleanup_error.add_note(
                    f"OpenSandbox context body also failed: {type(exc).__name__}: {exc}"
                )
                raise
            exc.add_note(
                "OpenSandbox context cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def start(self) -> None:
        """Idempotently open the State and complete initial warm-pool filling.

        Concurrent callers await the same startup task. A warm creation failure is
        logged and does not disable on-demand creation unless strict startup warmup
        was configured.

        Raises:
            OpenSandboxManagerClosedError: The manager has begun closing.
            OpenSandboxStateError: The allocation State cannot start safely.
        """
        async with self._state_lock:
            if self._closed:
                raise OpenSandboxManagerClosedError("OpenSandbox manager is closed")
            start_task = self._start_task
            if start_task is None:
                start_task = asyncio.create_task(self._start_resources())
                self._start_task = start_task
        await asyncio.shield(start_task)

    async def _start_resources(self) -> None:
        """Initialize authoritative State before starting dependent shared tasks."""
        await self._state.start(warm_pool_size=self._warm_pool_size)
        self._started = True
        retry_cleanup = await self._drain_cleanup_queue()
        self._cleanup_queue_task = asyncio.create_task(
            self._cleanup_queue_loop(retry_pending=retry_cleanup),
            name="tinkerfin-opensandbox-cleanup",
        )
        await self._fill_warm_pool(fail_on_error=self._fail_on_startup_warmup_error)

    def _ensure_open(self) -> None:
        if self._closed:
            raise OpenSandboxManagerClosedError("OpenSandbox manager is closed")

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        """Register a public operation so closure waits for its complete exit."""
        async with self._state_lock:
            self._ensure_open()
            self._active_operations += 1
            self._operations_done.clear()
        try:
            yield
        finally:
            async with self._state_lock:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._operations_done.set()

    @asynccontextmanager
    async def _renew_owner_claim(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> AsyncIterator[None]:
        """Renew an owner claim and stop the operation immediately if ownership is lost."""
        interval = self._state.lease_renew_interval
        if interval is None:
            yield
            return

        operation_task = asyncio.current_task()
        if operation_task is None:
            raise RuntimeError("OpenSandbox owner claims require an asyncio task")
        closing = False
        renewal_failure: Exception | None = None

        async def renew() -> None:
            nonlocal renewal_failure
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self._state.renew_owner(claim)
                except asyncio.CancelledError:
                    raise
                # State is host-replaceable, so any renewal failure makes claim
                # ownership unverifiable.
                except Exception as exc:  # noqa: BLE001
                    if closing:
                        return
                    renewal_failure = exc
                    operation_task.cancel()
                    return
                if renewed:
                    continue
                if closing:
                    return
                renewal_failure = OpenSandboxStateOwnershipError(
                    f"Owner claim for {claim.owner_key!r} was lost during the operation"
                )
                operation_task.cancel()
                return

        renewal_task = asyncio.create_task(
            renew(),
            name=f"tinkerfin-opensandbox-owner-lease:{claim.owner_digest}",
        )
        try:
            try:
                yield
            finally:
                closing = True
                if not renewal_task.done():
                    renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)
        except asyncio.CancelledError as cancellation:
            if renewal_failure is None:
                raise cancellation
            if isinstance(renewal_failure, OpenSandboxStateError):
                raise renewal_failure
            raise OpenSandboxStateError(
                f"Renewing owner claim for {claim.owner_key!r} failed"
            ) from renewal_failure

    @asynccontextmanager
    async def _claim_owner(
        self,
        owner_key: str,
    ) -> AsyncIterator[OpenSandboxOwnerClaim]:
        """Acquire the unique fencing claim for one owner from State."""
        claim = await self._state.acquire_owner(owner_key)
        try:
            async with self._renew_owner_claim(claim):
                yield claim
        finally:
            release_task = asyncio.create_task(
                self._state.release_owner(claim),
                name=f"tinkerfin-opensandbox-owner-release:{claim.owner_digest}",
            )
            await self._await_claim_release(release_task)

    async def _create_for_warm_claim(
        self,
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

    async def _is_backend_healthy(self, backend: _HealthBackend) -> bool:
        """Treat nonzero health results and all probe failures as unhealthy."""
        try:
            response = await backend.aexecute(
                self._client.config.health_command,
            )
            return response.exit_code == 0
        except Exception:
            logger.info("Sandbox %s health check failed", backend.id, exc_info=True)
            return False

    async def _renew_backend(self, backend: _HealthBackend) -> None:
        """Best-effort renewal without invalidating an otherwise healthy handle."""
        try:
            await backend.arenew(self._client.config.ttl)
        except Exception:
            logger.warning("Failed to renew Sandbox %s", backend.id, exc_info=True)

    async def _close_backend(self, backend: OpenSandboxBackend) -> None:
        """Best-effort local closure that does not mask the primary result."""
        try:
            await backend.aclose()
        except Exception:
            logger.warning(
                "Failed to close local resources for Sandbox %s",
                backend.id,
                exc_info=True,
            )

    async def _close_handle(self, handle: OpenSandboxHandle) -> None:
        """Retire a handle and close its local backend through the manager."""
        try:
            await handle._aclose_from_manager()
        except Exception:
            logger.warning(
                "Failed to close Sandbox handle %s", handle.id, exc_info=True
            )

    def _track_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Retain a cleanup task until completion so ``aclose`` can await it."""
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _start_backend_cleanup(
        self,
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
        self,
        backend: OpenSandboxBackend,
        *,
        destroy: bool,
    ) -> None:
        """Shield cleanup so a currently owned backend is always disposed."""
        await asyncio.shield(self._start_backend_cleanup(backend, destroy=destroy))

    async def _cleanup_after_health_check(
        self,
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
        self,
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

    async def _wait_for_cleanup_tasks(self) -> None:
        """Wait for current cleanup tasks and any tasks they derive.

        Removing each completed snapshot explicitly avoids a busy loop when task done
        callbacks have not yet removed those same tasks from the retained set.
        """
        while self._cleanup_tasks:
            tasks = tuple(self._cleanup_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._cleanup_tasks.difference_update(tasks)

    async def _destroy_remote(
        self,
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
            logger.warning(
                "Failed to destroy remote Sandbox %s", sandbox_id, exc_info=True
            )

    async def _drain_cleanup_queue(self) -> bool:
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
        self,
        claim: OpenSandboxCleanupClaim,
    ) -> None:
        """Settle cleanup-claim release before propagating caller cancellation."""
        release_task = asyncio.create_task(
            self._state.release_cleanup(claim),
            name=f"tinkerfin-opensandbox-cleanup-release:{claim.sandbox_id}",
        )
        await self._await_claim_release(release_task)

    @staticmethod
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
        self,
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

    async def _cleanup_queue_loop(self, *, retry_pending: bool) -> None:
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
        self,
        backend: OpenSandboxBackend,
        *,
        strict: bool = False,
    ) -> None:
        """Attempt remote destruction before unconditionally closing locally."""
        try:
            await self._destroy_remote(backend.id, strict=strict)
        finally:
            await self._close_backend(backend)

    async def _fill_warm_pool(self, *, fail_on_error: bool = False) -> None:
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

    def _schedule_replenish(self) -> None:
        """Schedule at most one replenishment task for an open undersized pool."""
        if self._closed or not self._started or self._warm_pool_size == 0:
            return
        if self._replenish_task is not None and not self._replenish_task.done():
            return
        self._replenish_task = asyncio.create_task(
            self._fill_warm_pool(fail_on_error=False)
        )

    async def _take_local_warm_backend(
        self,
        sandbox_id: str,
    ) -> OpenSandboxBackend | None:
        """Take the local backend retained for one shared warm slot."""
        async with self._warm_lock:
            for index, backend in enumerate(self._warm_backends):
                if backend.id == sandbox_id:
                    return self._warm_backends.pop(index)
        return None

    async def _acquire_backend(
        self,
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
            if (
                current_binding_id is not None
                and current_binding_id != binding.sandbox_id
            ):
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

    async def _reconcile_candidate_binding(
        self,
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
        self,
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
        self,
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
        self,
        handle: OpenSandboxHandle,
        backend: OpenSandboxBackend,
    ) -> None:
        """Destroy and close an old backend after its in-flight calls exit."""
        await handle._await_until_idle(backend)
        await self._dispose_backend(backend)

    def _workspace_root(self) -> str | None:
        """Read and validate the client-declared model-visible workspace root."""
        value = getattr(self._client.config, "workspace_root", None)
        return _normalize_workspace_root(value)

    def _backend_view(
        self,
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
        self,
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

    async def get(self, key: KeyT) -> _ManagedBackend:
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
        self,
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
            except Exception:
                logger.info(
                    "Failed to reconnect Sandbox %s; creating a replacement",
                    stored_id,
                    exc_info=True,
                )
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

        replaceable_handle = (
            handle if handle is not None and not handle.is_closed else None
        )
        return await self._replace(
            owner_key,
            claim,
            replaceable_handle,
            old_id=handle.id if handle is not None else None,
        )

    async def _close_replaced_backend(
        self,
        handle: OpenSandboxHandle,
        backend: OpenSandboxBackend,
    ) -> None:
        """Close an idle old connection without destroying its rebound remote instance."""
        await handle._await_until_idle(backend)
        await self._close_backend(backend)

    async def recreate(self, key: KeyT) -> _ManagedBackend:
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

    async def reconnect(self, key: KeyT) -> _ManagedBackend:
        """Alias for ``get()`` that also creates when no binding exists."""
        return await self.get(key)

    async def reset(self, key: KeyT) -> None:
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
                    except Exception:
                        logger.info(
                            "Workspace reset for %r failed after caller cancellation",
                            owner_key,
                            exc_info=True,
                        )
                    raise cancellation
                except Exception as exc:
                    raise OpenSandboxResetError(
                        f"Failed to clear the OpenSandbox workspace for {owner_key!r}"
                    ) from exc

    async def destroy(self, key: KeyT) -> None:
        """Destroy known remote instances and remove the committed binding."""
        await self.delete(key)

    async def is_healthy(self, key: KeyT) -> bool:
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

    async def get_details(self, key: KeyT) -> OpenSandboxDetails | None:
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
        self,
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

    async def delete(self, key: KeyT) -> None:
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
                    except Exception:
                        logger.info(
                            "Sandbox deletion for %r failed after caller cancellation; "
                            "targets retained",
                            owner_key,
                            exc_info=True,
                        )
                    raise cancellation

    async def _close_resources(self) -> None:
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

    async def aclose(self) -> None:
        """Idempotently close every resource owned by this manager.

        All callers await one shielded close task. Process-local State resources are
        destroyed; persistent bindings, shared warm slots, and cleanup work remain
        available to another worker. The client and State are both closed. A finite
        ``settlement_timeout`` limits only the current caller's wait; the same close
        task remains owned and can be awaited by calling ``aclose`` again.

        Raises:
            OpenSandboxSettlementTimeoutError: The configured caller wait expires
                before the shared close task settles.
        """
        async with self._state_lock:
            close_task = self._close_task
            if close_task is None:
                self._closed = True
                self._cleanup_wakeup.set()
                close_task = asyncio.create_task(
                    self._close_resources(),
                    name="tinkerfin-opensandbox-close",
                )
                self._close_task = close_task
                close_task.add_done_callback(self._close_finished)
        timeout = self._settlement_timeout
        if timeout is None:
            await asyncio.shield(close_task)
            return
        if close_task.done():
            close_task.result()
            return
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                await asyncio.shield(close_task)
        except TimeoutError as error:
            if deadline.expired():
                raise OpenSandboxSettlementTimeoutError(timeout=timeout) from error
            raise

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained close failure when no caller waits again."""

        if not task.cancelled():
            task.exception()
