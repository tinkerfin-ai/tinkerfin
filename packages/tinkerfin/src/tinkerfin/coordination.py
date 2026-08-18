"""Per-principal coordination for Graph runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ._tasks import join_task

PrincipalT = TypeVar("PrincipalT")
PrincipalT_contra = TypeVar("PrincipalT_contra", contravariant=True)


@runtime_checkable
class RunCoordinator(Protocol[PrincipalT_contra]):
    """Provide one asynchronous exclusive scope for a caller-defined principal."""

    def __call__(
        self,
        principal: PrincipalT_contra,
        /,
    ) -> AbstractAsyncContextManager[None]: ...


class _LockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class InMemoryRunCoordinator(Generic[PrincipalT]):
    """Serialize Graph runs that resolve to the same process-local key.

    The coordinator borrows no resources and is intended for one event loop. Use the
    Redis implementation when workers or processes must share the same run boundary.

    Args:
        key_resolver: Convert an opaque principal into a stable, non-blank key.
    """

    def __init__(self, *, key_resolver: Callable[[PrincipalT], str]) -> None:
        if not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        self._key_resolver = key_resolver
        self._entries: dict[str, _LockEntry] = {}
        self._entries_lock = asyncio.Lock()
        self._settlement_tasks: set[asyncio.Task[None]] = set()

    def __call__(
        self,
        principal: PrincipalT,
        /,
    ) -> AbstractAsyncContextManager[None]:
        return self._coordinate(principal)

    @asynccontextmanager
    async def _coordinate(self, principal: PrincipalT) -> AsyncIterator[None]:
        key = self._key_resolver(principal)
        if not isinstance(key, str):
            raise TypeError("key_resolver must return a string")
        if not key or key != key.strip():
            raise ValueError(
                "key_resolver must return a non-blank string without surrounding "
                "whitespace"
            )

        async with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry()
                self._entries[key] = entry
            entry.users += 1

        acquired = False
        primary: BaseException | None = None
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        except BaseException as error:  # noqa: BLE001 - preserve scope outcome
            primary = error

        if acquired:
            entry.lock.release()

        settlement = asyncio.create_task(
            self._settle_entry(key, entry),
            name="tinkerfin-run-coordinator-settlement",
        )
        self._settlement_tasks.add(settlement)
        settlement.add_done_callback(self._settlement_finished)
        try:
            await join_task(settlement)
        except asyncio.CancelledError as cancellation:
            if isinstance(primary, asyncio.CancelledError):
                primary.add_note(
                    "run coordination settlement received another caller cancellation: "
                    f"{cancellation}"
                )
                for note in getattr(cancellation, "__notes__", ()):
                    primary.add_note(note)
            else:
                if primary is not None:
                    cancellation.add_note(
                        "coordinated scope also failed: "
                        f"{type(primary).__name__}: {primary}"
                    )
                primary = cancellation
        except BaseException as settlement_error:  # noqa: BLE001 - owned settlement
            if primary is None:
                primary = settlement_error
            else:
                primary.add_note(
                    "run coordination settlement also failed: "
                    f"{type(settlement_error).__name__}: {settlement_error}"
                )

        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)

    async def _settle_entry(self, key: str, entry: _LockEntry) -> None:
        """Release one registered user without exposing cancellation to the caller."""

        async with self._entries_lock:
            entry.users -= 1
            if entry.users == 0 and self._entries.get(key) is entry:
                self._entries.pop(key)

    def _settlement_finished(self, task: asyncio.Task[None]) -> None:
        """Drop and consume a completed settlement task after every caller leaves."""

        self._settlement_tasks.discard(task)
        if not task.cancelled():
            task.exception()


__all__ = ["InMemoryRunCoordinator", "RunCoordinator"]
