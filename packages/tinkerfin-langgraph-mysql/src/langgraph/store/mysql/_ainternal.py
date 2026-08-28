"""Private asynchronous connection protocols used by the MySQL Store."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Generic, Protocol, Self, TypeVar, cast


class AsyncDictCursor(Protocol):
    """Describe the asynchronous dictionary cursor required by Store queries."""

    async def execute(
        self,
        operation: str,
        parameters: Sequence[Any] | Mapping[str, Any] = ...,
        /,
    ) -> object:
        """Execute one parameterized SQL statement."""
        ...

    async def executemany(
        self, operation: str, seq_of_parameters: Sequence[Sequence[Any]], /
    ) -> object:
        """Execute one statement for each parameter sequence."""
        ...

    async def fetchone(self) -> dict[str, Any] | None:
        """Return the next row or ``None`` when exhausted."""
        ...

    async def fetchall(self) -> Sequence[dict[str, Any]]:
        """Return every remaining row in driver order."""
        ...

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        """Iterate result rows asynchronously."""
        ...

    async def __aenter__(self) -> Self:
        """Enter the driver cursor context."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> object:
        """Release the driver cursor without suppressing caller failures."""
        ...


R = TypeVar("R", bound=AsyncDictCursor)


class AsyncConnection(Protocol):
    """Describe the transaction and cursor surface required from a connection."""

    async def begin(self) -> None:
        """Begin a transaction."""
        ...

    async def commit(self) -> None:
        """Commit changes to stable storage."""
        ...

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        ...

    async def set_charset(self, charset: str) -> None:
        """Set the character set for the current connection."""
        ...


C = TypeVar("C", bound=AsyncConnection)
COut = TypeVar("COut", bound=AsyncConnection, covariant=True)


class AsyncConnectionLease(Protocol, Generic[COut]):
    """Yield one acquired connection and release it when the context exits."""

    async def __aenter__(self) -> COut: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> object: ...


class AsyncPool(Protocol, Generic[COut]):
    """Describe a caller-owned pool that lends connection contexts."""

    def acquire(self) -> AsyncConnectionLease[COut]:
        """Acquire one connection context from the pool."""
        ...


Conn = C | AsyncPool[C]


@asynccontextmanager
async def get_connection(
    conn: Conn[C],
) -> AsyncGenerator[C, None]:
    """Yield a borrowed direct connection or one lease from a borrowed pool.

    Args:
        conn: Direct connection or pool supplied by the caller.

    Yields:
        One connection valid only within the current context.

    Raises:
        TypeError: The value exposes neither a cursor nor a pool acquisition API.
    """

    if hasattr(conn, "cursor"):
        yield cast(C, conn)
    elif hasattr(conn, "acquire"):
        async with cast(AsyncPool[C], conn).acquire() as connection:
            yield connection
    else:
        raise TypeError(f"Invalid connection type: {type(conn)}")
