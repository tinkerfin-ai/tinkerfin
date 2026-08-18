"""State contracts for durable OpenSandbox ownership transitions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import uuid4

from ..errors import (
    OpenSandboxStateConfigurationError,
    OpenSandboxStateOwnershipError,
)


def _owner_digest(namespace: str, owner_key: str) -> str:
    """Return a stable opaque digest without persisting the raw owner key."""
    value = f"{namespace}\0{owner_key}".encode()
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
    )


@dataclass(frozen=True, slots=True)
class OpenSandboxBinding:
    """Committed mapping from one owner to one remote Sandbox generation."""

    sandbox_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class OpenSandboxOwnerClaim:
    """Fencing identity that exclusively owns one owner state transition."""

    owner_key: str
    owner_digest: str
    token: str
    generation: int
    binding: OpenSandboxBinding | None


@dataclass(frozen=True, slots=True)
class OpenSandboxWarmClaim:
    """Fencing identity for filling one global warm-pool slot."""

    slot: int
    token: str
    generation: int


@dataclass(frozen=True, slots=True)
class OpenSandboxCleanupClaim:
    """Fencing identity for destroying one orphaned remote Sandbox."""

    sandbox_id: str
    token: str
    generation: int


@runtime_checkable
class OpenSandboxState(Protocol):
    """Atomic owner allocation state used by ``OpenSandboxManager``.

    Implementations serialize transitions for the same owner while allowing
    unrelated owners to proceed independently. A claim is a fencing identity;
    mutations made with a released or superseded claim must fail.
    """

    @property
    def persistent(self) -> bool: ...

    @property
    def lease_renew_interval(self) -> float | None: ...

    async def start(self, *, warm_pool_size: int) -> None: ...

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim: ...

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool: ...

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
    ) -> OpenSandboxBinding: ...

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None: ...

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None: ...

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None: ...

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None: ...

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None: ...

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool: ...

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None: ...

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume a slot and commit its Sandbox as the owner binding.

        A non-``None`` result is already authoritative. Callers must publish that
        exact binding without invoking ``bind_owner()`` again.
        """

        ...

    async def enqueue_cleanup(self, sandbox_id: str) -> None: ...

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None: ...

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool: ...

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None: ...

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None: ...

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class _MemoryOwnerRecord:
    lock: asyncio.Lock
    generation: int = 0
    binding: OpenSandboxBinding | None = None
    active_token: str | None = None


@dataclass(slots=True)
class _MemoryWarmSlot:
    slot: int
    generation: int = 0
    sandbox_id: str | None = None
    active_token: str | None = None


@dataclass(slots=True)
class _MemoryCleanupRecord:
    generation: int = 0
    active_token: str | None = None


class InMemoryOpenSandboxState(OpenSandboxState):
    """Keep owner allocation state inside one Python process."""

    def __init__(self, *, namespace: str = "") -> None:
        self._namespace = namespace
        self._records: dict[str, _MemoryOwnerRecord] = {}
        self._records_guard = asyncio.Lock()
        self._warm_slots: list[_MemoryWarmSlot] = []
        self._warm_pool_size: int | None = None
        self._cleanup: dict[str, _MemoryCleanupRecord] = {}
        self._started = False
        self._closed = False

    @property
    def persistent(self) -> bool:
        """Process-local resources are not durable across manager shutdown."""
        return False

    @property
    def lease_renew_interval(self) -> None:
        """In-process claims do not expire while their State remains alive."""
        return None

    async def start(self, *, warm_pool_size: int) -> None:
        """Open the state; repeated calls require the original warm-pool capacity."""
        if warm_pool_size < 0:
            raise ValueError("warm_pool_size must not be negative")
        if self._closed:
            raise RuntimeError("OpenSandbox state is closed")
        if self._started:
            if self._warm_pool_size != warm_pool_size:
                raise OpenSandboxStateConfigurationError(
                    "OpenSandbox State is already started with a different "
                    "warm_pool_size"
                )
            return
        self._warm_slots = [
            _MemoryWarmSlot(slot=index) for index in range(warm_pool_size)
        ]
        self._warm_pool_size = warm_pool_size
        self._started = True

    def _ensure_open(self) -> None:
        if not self._started:
            raise RuntimeError("OpenSandbox state has not been started")
        if self._closed:
            raise RuntimeError("OpenSandbox state is closed")

    async def _record(self, owner_key: str) -> tuple[str, _MemoryOwnerRecord]:
        self._ensure_open()
        digest = _owner_digest(self._namespace, owner_key)
        async with self._records_guard:
            record = self._records.get(digest)
            if record is None:
                record = _MemoryOwnerRecord(lock=asyncio.Lock())
                self._records[digest] = record
        return digest, record

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        """Wait for and exclusively claim one owner's next transition."""
        digest, record = await self._record(owner_key)
        await record.lock.acquire()
        try:
            self._ensure_open()
            record.generation += 1
            token = uuid4().hex
            record.active_token = token
            return OpenSandboxOwnerClaim(
                owner_key=owner_key,
                owner_digest=digest,
                token=token,
                generation=record.generation,
                binding=record.binding,
            )
        except BaseException:
            record.lock.release()
            raise

    def _claimed_record(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> _MemoryOwnerRecord:
        self._ensure_open()
        record = self._records.get(claim.owner_digest)
        if (
            record is None
            or record.active_token != claim.token
            or record.generation != claim.generation
        ):
            raise OpenSandboxStateOwnershipError(
                f"Owner claim for {claim.owner_key!r} is no longer current"
            )
        return record

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
    ) -> OpenSandboxBinding:
        """Commit a Sandbox ID only for the current fencing claim."""
        record = self._claimed_record(claim)
        binding = OpenSandboxBinding(
            sandbox_id=sandbox_id,
            generation=claim.generation,
        )
        record.binding = binding
        return binding

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        """Confirm that an in-process owner claim is still current."""
        try:
            self._claimed_record(claim)
        except OpenSandboxStateOwnershipError:
            return False
        return True

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        """Read the latest committed binding after any active transition."""
        _, record = await self._record(owner_key)
        async with record.lock:
            return record.binding

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Remove the binding protected by the current owner claim."""
        record = self._claimed_record(claim)
        record.binding = None

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Release a current claim; stale releases cannot unlock a successor."""
        record = self._records.get(claim.owner_digest)
        if record is None or record.active_token != claim.token:
            return
        record.active_token = None
        record.lock.release()

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        """Claim one empty global slot without waiting for remote creation."""
        self._ensure_open()
        for slot in self._warm_slots:
            if slot.sandbox_id is None and slot.active_token is None:
                slot.generation += 1
                token = uuid4().hex
                slot.active_token = token
                return OpenSandboxWarmClaim(
                    slot=slot.slot,
                    token=token,
                    generation=slot.generation,
                )
        return None

    def _claimed_warm_slot(
        self,
        claim: OpenSandboxWarmClaim,
    ) -> _MemoryWarmSlot:
        self._ensure_open()
        try:
            slot = self._warm_slots[claim.slot]
        except IndexError as exc:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} is no longer current"
            ) from exc
        if slot.active_token != claim.token or slot.generation != claim.generation:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} is no longer current"
            )
        return slot

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        """Publish a created Sandbox into its claimed global warm slot."""
        slot = self._claimed_warm_slot(claim)
        slot.sandbox_id = sandbox_id
        slot.active_token = None

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        """Confirm that an in-process warm claim is still current."""
        try:
            self._claimed_warm_slot(claim)
        except OpenSandboxStateOwnershipError:
            return False
        return True

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        """Release an unfilled warm claim without affecting a successor."""
        if claim.slot >= len(self._warm_slots):
            return
        slot = self._warm_slots[claim.slot]
        if slot.active_token == claim.token:
            slot.active_token = None

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume and authoritatively bind one ready warm Sandbox."""
        owner = self._claimed_record(claim)
        for slot in self._warm_slots:
            if slot.sandbox_id is None or slot.active_token is not None:
                continue
            binding = OpenSandboxBinding(
                sandbox_id=slot.sandbox_id,
                generation=claim.generation,
            )
            slot.sandbox_id = None
            owner.binding = binding
            return binding
        return None

    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        """Persist an idempotent orphan cleanup target."""
        self._ensure_open()
        self._cleanup.setdefault(sandbox_id, _MemoryCleanupRecord())

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        """Claim one pending cleanup target without blocking."""
        self._ensure_open()
        for sandbox_id, record in self._cleanup.items():
            if record.active_token is not None:
                continue
            record.generation += 1
            token = uuid4().hex
            record.active_token = token
            return OpenSandboxCleanupClaim(
                sandbox_id=sandbox_id,
                token=token,
                generation=record.generation,
            )
        return None

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        """Confirm that an in-process cleanup claim is still current."""
        try:
            self._claimed_cleanup(claim)
        except OpenSandboxStateOwnershipError:
            return False
        return True

    def _claimed_cleanup(
        self,
        claim: OpenSandboxCleanupClaim,
    ) -> _MemoryCleanupRecord:
        self._ensure_open()
        record = self._cleanup.get(claim.sandbox_id)
        if (
            record is None
            or record.active_token != claim.token
            or record.generation != claim.generation
        ):
            raise OpenSandboxStateOwnershipError(
                f"Cleanup claim for {claim.sandbox_id!r} is no longer current"
            )
        return record

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Remove a cleanup target after confirmed remote destruction."""
        self._claimed_cleanup(claim)
        self._cleanup.pop(claim.sandbox_id, None)

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Release a failed cleanup target for a later retry."""
        record = self._cleanup.get(claim.sandbox_id)
        if record is not None and record.active_token == claim.token:
            record.active_token = None

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        """Return every remote resource exclusively owned by this memory State."""
        self._ensure_open()
        sandbox_ids = {
            record.binding.sandbox_id
            for record in self._records.values()
            if record.binding is not None
        }
        sandbox_ids.update(
            slot.sandbox_id for slot in self._warm_slots if slot.sandbox_id is not None
        )
        sandbox_ids.update(self._cleanup)
        return tuple(sorted(sandbox_ids))

    async def aclose(self) -> None:
        """Close this process-local state idempotently."""
        self._closed = True
