"""SQLAlchemy owner, warm-slot, and cleanup state operations."""

from __future__ import annotations

__all__ = ["_enqueue_cleanup_in_transaction"]

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import OpenSandboxStateOwnershipError
from ._sql_schema import _cleanup, _owners, _warm_slots
from ._sql_transactions import _apply_claim_lock
from .state import (
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxReadyWarmClaim,
    OpenSandboxWarmClaim,
    _owner_digest,
)

if TYPE_CHECKING:
    from .sqlalchemy import SQLAlchemyOpenSandboxState


async def acquire_owner(
    self: SQLAlchemyOpenSandboxState, owner_key: str
) -> OpenSandboxOwnerClaim:
    """Wait until this worker atomically owns the next owner generation."""
    self._ensure_open()
    digest = _owner_digest(self._namespace, owner_key)
    while True:
        token = uuid4().hex

        async def acquire(
            connection: AsyncConnection,
        ) -> OpenSandboxOwnerClaim | None:
            now = self._now()
            expires_at = now + timedelta(seconds=self._lease_ttl)
            statement = select(_owners).where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == digest,
            )
            if self._require_capabilities().name == "mysql":
                statement = statement.with_for_update()
            row = (await connection.execute(statement)).mappings().one_or_none()
            if row is None:
                await connection.execute(
                    insert(_owners).values(
                        namespace=self._namespace,
                        owner_digest=digest,
                        sandbox_id=None,
                        binding_generation=0,
                        generation=1,
                        claim_token=token,
                        lease_expires_at=expires_at,
                        updated_at=now,
                    )
                )
                return OpenSandboxOwnerClaim(
                    owner_key=owner_key,
                    owner_digest=digest,
                    token=token,
                    generation=1,
                    binding=None,
                )
            lease_expires_at = row["lease_expires_at"]
            if row["claim_token"] is not None and not (
                lease_expires_at is not None and lease_expires_at <= now
            ):
                return None
            generation = int(row["generation"]) + 1
            result = await connection.execute(
                update(_owners)
                .where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == digest,
                    _owners.c.generation == row["generation"],
                )
                .values(
                    generation=generation,
                    claim_token=token,
                    lease_expires_at=expires_at,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            sandbox_id = row["sandbox_id"]
            binding = (
                OpenSandboxBinding(
                    sandbox_id=str(sandbox_id),
                    generation=int(row["binding_generation"]),
                )
                if sandbox_id is not None
                else None
            )
            return OpenSandboxOwnerClaim(
                owner_key=owner_key,
                owner_digest=digest,
                token=token,
                generation=generation,
                binding=binding,
            )

        try:
            claim = await self._run_write_transaction(acquire)
        except DBAPIError as exc:
            if not self._is_retryable_mysql_conflict(exc):
                raise
        else:
            if claim is not None:
                return claim
        await asyncio.sleep(self._poll_interval)


async def bind_owner(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxOwnerClaim,
    sandbox_id: str,
) -> OpenSandboxBinding:
    """Commit a binding with claim token, generation, and lease fencing."""
    self._ensure_open()

    async def bind(connection: AsyncConnection) -> None:
        now = self._now()
        result = await connection.execute(
            update(_owners)
            .where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == claim.owner_digest,
                _owners.c.claim_token == claim.token,
                _owners.c.generation == claim.generation,
                _owners.c.lease_expires_at > now,
            )
            .values(
                sandbox_id=sandbox_id,
                binding_generation=claim.generation,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Owner claim for {claim.owner_key!r} is no longer current"
            )

    await self._run_write_transaction(bind)
    return OpenSandboxBinding(
        sandbox_id=sandbox_id,
        generation=claim.generation,
    )


async def renew_owner(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxOwnerClaim
) -> bool:
    """Extend a current, unexpired owner lease."""
    self._ensure_open()

    async def renew(connection: AsyncConnection) -> bool:
        now = self._now()
        result = await connection.execute(
            update(_owners)
            .where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == claim.owner_digest,
                _owners.c.claim_token == claim.token,
                _owners.c.generation == claim.generation,
                _owners.c.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                updated_at=now,
            )
        )
        return result.rowcount == 1

    return await self._run_write_transaction(renew)


async def unbind_owner(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxOwnerClaim
) -> None:
    """Remove a binding only while the owner claim remains current."""
    self._ensure_open()

    async def unbind(connection: AsyncConnection) -> None:
        now = self._now()
        result = await connection.execute(
            update(_owners)
            .where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == claim.owner_digest,
                _owners.c.claim_token == claim.token,
                _owners.c.generation == claim.generation,
                _owners.c.lease_expires_at > now,
            )
            .values(
                sandbox_id=None,
                binding_generation=claim.generation,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Owner claim for {claim.owner_key!r} is no longer current"
            )

    await self._run_write_transaction(unbind)


async def read_binding(
    self: SQLAlchemyOpenSandboxState, owner_key: str
) -> OpenSandboxBinding | None:
    """Read the latest committed owner binding without claiming it."""
    self._ensure_open()
    digest = _owner_digest(self._namespace, owner_key)
    async with self._engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    _owners.c.sandbox_id,
                    _owners.c.binding_generation,
                ).where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == digest,
                )
            )
        ).one_or_none()
    if row is None or row.sandbox_id is None:
        return None
    return OpenSandboxBinding(
        sandbox_id=str(row.sandbox_id),
        generation=int(row.binding_generation),
    )


async def release_owner(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxOwnerClaim
) -> None:
    """Release only the exact owner claim supplied by the caller."""
    self._ensure_open()

    async def release(connection: AsyncConnection) -> None:
        await connection.execute(
            update(_owners)
            .where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == claim.owner_digest,
                _owners.c.claim_token == claim.token,
                _owners.c.generation == claim.generation,
            )
            .values(
                claim_token=None,
                lease_expires_at=None,
                updated_at=self._now(),
            )
        )

    await self._run_write_transaction(release)


async def claim_warm_slot(
    self: SQLAlchemyOpenSandboxState,
) -> OpenSandboxWarmClaim | None:
    """Claim one empty or abandoned global warm-pool slot."""
    self._ensure_open()

    async def claim(connection: AsyncConnection) -> OpenSandboxWarmClaim | None:
        now = self._now()
        statement = (
            select(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.sandbox_id.is_(None),
                (_warm_slots.c.claim_token.is_(None))
                | (_warm_slots.c.lease_expires_at <= now),
            )
            .order_by(_warm_slots.c.slot)
            .limit(1)
        )
        capabilities = self._require_capabilities()
        statement = _apply_claim_lock(
            statement,
            capabilities=capabilities,
        )
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        token = uuid4().hex
        generation = int(row["generation"]) + 1
        result = await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == row["slot"],
                _warm_slots.c.generation == row["generation"],
            )
            .values(
                generation=generation,
                claim_token=token,
                lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return OpenSandboxWarmClaim(
            slot=int(row["slot"]),
            token=token,
            generation=generation,
        )

    return await self._run_claim_transaction(claim)


async def claim_ready_warm_slot(
    self: SQLAlchemyOpenSandboxState,
    *,
    exclude_slots: Sequence[int],
) -> OpenSandboxReadyWarmClaim | None:
    """Fence one published warm slot without discarding its current remote ID."""

    self._ensure_open()

    async def claim(connection: AsyncConnection) -> OpenSandboxReadyWarmClaim | None:
        now = self._now()
        statement = select(_warm_slots).where(
            _warm_slots.c.namespace == self._namespace,
            _warm_slots.c.sandbox_id.is_not(None),
            (_warm_slots.c.claim_token.is_(None))
            | (_warm_slots.c.lease_expires_at <= now),
        )
        if exclude_slots:
            statement = statement.where(_warm_slots.c.slot.not_in(tuple(exclude_slots)))
        statement = statement.order_by(_warm_slots.c.slot).limit(1)
        statement = _apply_claim_lock(
            statement,
            capabilities=self._require_capabilities(),
        )
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        token = uuid4().hex
        generation = int(row["generation"]) + 1
        result = await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == row["slot"],
                _warm_slots.c.generation == row["generation"],
                _warm_slots.c.sandbox_id == row["sandbox_id"],
            )
            .values(
                generation=generation,
                claim_token=token,
                lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return OpenSandboxReadyWarmClaim(
            slot=int(row["slot"]),
            token=token,
            generation=generation,
            sandbox_id=str(row["sandbox_id"]),
        )

    return await self._run_claim_transaction(claim)


async def discard_ready_warm_slot(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxReadyWarmClaim,
) -> None:
    """Clear one unusable published ID and enqueue its cleanup atomically."""

    self._ensure_open()

    async def discard(connection: AsyncConnection) -> None:
        now = self._now()
        result = await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == claim.slot,
                _warm_slots.c.sandbox_id == claim.sandbox_id,
                _warm_slots.c.claim_token == claim.token,
                _warm_slots.c.generation == claim.generation,
                _warm_slots.c.lease_expires_at > now,
            )
            .values(
                sandbox_id=None,
                claim_token=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} is no longer current"
            )
        await self._enqueue_cleanup_in_transaction(
            connection,
            claim.sandbox_id,
            now=now,
        )

    await self._run_write_transaction(discard)


async def warm_pool_ready(self: SQLAlchemyOpenSandboxState) -> bool:
    """Return whether every configured slot has an unclaimed published Sandbox."""

    self._ensure_open()
    async with self._engine.connect() as connection:
        ready = await connection.scalar(
            select(func.count())
            .select_from(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.sandbox_id.is_not(None),
                _warm_slots.c.claim_token.is_(None),
            )
        )
    return int(ready or 0) == self._warm_pool_size


async def publish_warm(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxWarmClaim,
    sandbox_id: str,
) -> None:
    """Publish a remote Sandbox only through the current warm claim."""
    self._ensure_open()

    async def publish(connection: AsyncConnection) -> None:
        now = self._now()
        result = await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == claim.slot,
                _warm_slots.c.claim_token == claim.token,
                _warm_slots.c.generation == claim.generation,
                _warm_slots.c.lease_expires_at > now,
            )
            .values(
                sandbox_id=sandbox_id,
                claim_token=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {claim.slot} is no longer current"
            )

    await self._run_write_transaction(publish)


async def renew_warm(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxWarmClaim
) -> bool:
    """Extend a current, unexpired warm-slot lease."""
    self._ensure_open()

    async def renew(connection: AsyncConnection) -> bool:
        now = self._now()
        result = await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == claim.slot,
                _warm_slots.c.claim_token == claim.token,
                _warm_slots.c.generation == claim.generation,
                _warm_slots.c.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                updated_at=now,
            )
        )
        return result.rowcount == 1

    return await self._run_write_transaction(renew)


async def release_warm(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxWarmClaim
) -> None:
    """Release only the exact uncommitted warm claim."""
    self._ensure_open()

    async def release(connection: AsyncConnection) -> None:
        await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == claim.slot,
                _warm_slots.c.claim_token == claim.token,
                _warm_slots.c.generation == claim.generation,
            )
            .values(
                claim_token=None,
                lease_expires_at=None,
                updated_at=self._now(),
            )
        )

    await self._run_write_transaction(release)


async def consume_warm(
    self: SQLAlchemyOpenSandboxState,
    claim: OpenSandboxOwnerClaim,
) -> OpenSandboxBinding | None:
    """Atomically consume and authoritatively bind one ready global slot."""
    self._ensure_open()

    async def consume(
        connection: AsyncConnection,
    ) -> OpenSandboxBinding | None:
        now = self._now()
        owner_statement = select(_owners.c.owner_digest).where(
            _owners.c.namespace == self._namespace,
            _owners.c.owner_digest == claim.owner_digest,
            _owners.c.claim_token == claim.token,
            _owners.c.generation == claim.generation,
            _owners.c.lease_expires_at > now,
        )
        capabilities = self._require_capabilities()
        if capabilities.name == "mysql":
            owner_statement = owner_statement.with_for_update()
        if (await connection.execute(owner_statement)).one_or_none() is None:
            raise OpenSandboxStateOwnershipError(
                f"Owner claim for {claim.owner_key!r} is no longer current"
            )
        slot_statement = (
            select(_warm_slots.c.slot, _warm_slots.c.sandbox_id)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.sandbox_id.is_not(None),
                _warm_slots.c.claim_token.is_(None),
            )
            .order_by(_warm_slots.c.slot)
            .limit(1)
        )
        slot_statement = _apply_claim_lock(
            slot_statement,
            capabilities=capabilities,
        )
        slot = (await connection.execute(slot_statement)).one_or_none()
        if slot is None:
            return None
        sandbox_id = str(slot.sandbox_id)
        result = await connection.execute(
            update(_warm_slots)
            .where(
                _warm_slots.c.namespace == self._namespace,
                _warm_slots.c.slot == slot.slot,
                _warm_slots.c.sandbox_id == sandbox_id,
            )
            .values(sandbox_id=None, updated_at=now)
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Warm slot {slot.slot} is no longer available"
            )
        result = await connection.execute(
            update(_owners)
            .where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == claim.owner_digest,
                _owners.c.claim_token == claim.token,
                _owners.c.generation == claim.generation,
                _owners.c.lease_expires_at > now,
            )
            .values(
                sandbox_id=sandbox_id,
                binding_generation=claim.generation,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Owner claim for {claim.owner_key!r} is no longer current"
            )
        return OpenSandboxBinding(
            sandbox_id=sandbox_id,
            generation=claim.generation,
        )

    return await self._run_claim_transaction(consume)


async def _enqueue_cleanup_in_transaction(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    sandbox_id: str,
    *,
    now: datetime,
) -> None:
    existing = await connection.scalar(
        select(_cleanup.c.sandbox_id).where(
            _cleanup.c.namespace == self._namespace,
            _cleanup.c.sandbox_id == sandbox_id,
        )
    )
    if existing is not None:
        return
    await connection.execute(
        insert(_cleanup).values(
            namespace=self._namespace,
            sandbox_id=sandbox_id,
            generation=0,
            claim_token=None,
            lease_expires_at=None,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )


async def enqueue_cleanup(self: SQLAlchemyOpenSandboxState, sandbox_id: str) -> None:
    """Persist an idempotent remote-destruction retry target."""
    self._ensure_open()

    async def enqueue(connection: AsyncConnection) -> None:
        await self._enqueue_cleanup_in_transaction(
            connection,
            sandbox_id,
            now=self._now(),
        )

    while True:
        try:
            await self._run_write_transaction(enqueue)
            return
        except DBAPIError as exc:
            if not self._is_retryable_mysql_conflict(exc):
                raise
            await asyncio.sleep(self._poll_interval)


async def claim_cleanup(
    self: SQLAlchemyOpenSandboxState,
) -> OpenSandboxCleanupClaim | None:
    """Claim one pending or abandoned cleanup target."""
    self._ensure_open()

    async def claim(
        connection: AsyncConnection,
    ) -> OpenSandboxCleanupClaim | None:
        now = self._now()
        statement = (
            select(_cleanup)
            .where(
                _cleanup.c.namespace == self._namespace,
                (_cleanup.c.claim_token.is_(None))
                | (_cleanup.c.lease_expires_at <= now),
            )
            .order_by(_cleanup.c.created_at, _cleanup.c.sandbox_id)
            .limit(1)
        )
        capabilities = self._require_capabilities()
        statement = _apply_claim_lock(
            statement,
            capabilities=capabilities,
        )
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        generation = int(row["generation"]) + 1
        token = uuid4().hex
        result = await connection.execute(
            update(_cleanup)
            .where(
                _cleanup.c.namespace == self._namespace,
                _cleanup.c.sandbox_id == row["sandbox_id"],
                _cleanup.c.generation == row["generation"],
            )
            .values(
                generation=generation,
                claim_token=token,
                lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                attempts=int(row["attempts"]) + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return OpenSandboxCleanupClaim(
            sandbox_id=str(row["sandbox_id"]),
            token=token,
            generation=generation,
        )

    return await self._run_claim_transaction(claim)


async def renew_cleanup(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxCleanupClaim
) -> bool:
    """Extend a current, unexpired cleanup lease."""
    self._ensure_open()

    async def renew(connection: AsyncConnection) -> bool:
        now = self._now()
        result = await connection.execute(
            update(_cleanup)
            .where(
                _cleanup.c.namespace == self._namespace,
                _cleanup.c.sandbox_id == claim.sandbox_id,
                _cleanup.c.claim_token == claim.token,
                _cleanup.c.generation == claim.generation,
                _cleanup.c.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                updated_at=now,
            )
        )
        return result.rowcount == 1

    return await self._run_write_transaction(renew)


async def complete_cleanup(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxCleanupClaim
) -> None:
    """Delete a cleanup row only after its claimant confirms destruction."""
    self._ensure_open()

    async def complete(connection: AsyncConnection) -> None:
        result = await connection.execute(
            delete(_cleanup).where(
                _cleanup.c.namespace == self._namespace,
                _cleanup.c.sandbox_id == claim.sandbox_id,
                _cleanup.c.claim_token == claim.token,
                _cleanup.c.generation == claim.generation,
            )
        )
        if result.rowcount != 1:
            raise OpenSandboxStateOwnershipError(
                f"Cleanup claim for {claim.sandbox_id!r} is no longer current"
            )

    await self._run_write_transaction(complete)


async def release_cleanup(
    self: SQLAlchemyOpenSandboxState, claim: OpenSandboxCleanupClaim
) -> None:
    """Release one failed cleanup claim for a future retry."""
    self._ensure_open()

    async def release(connection: AsyncConnection) -> None:
        await connection.execute(
            update(_cleanup)
            .where(
                _cleanup.c.namespace == self._namespace,
                _cleanup.c.sandbox_id == claim.sandbox_id,
                _cleanup.c.claim_token == claim.token,
                _cleanup.c.generation == claim.generation,
            )
            .values(
                claim_token=None,
                lease_expires_at=None,
                updated_at=self._now(),
            )
        )

    await self._run_write_transaction(release)


async def shutdown_sandbox_ids(
    self: SQLAlchemyOpenSandboxState,
) -> tuple[str, ...]:
    """Keep durable bindings, warm slots, and cleanup work on shutdown."""
    self._ensure_open()
    return ()
