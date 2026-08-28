"""Asyncmy-owned connection lifecycle for the current LangGraph MySQL Store."""

import urllib.parse
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Protocol, Self, cast

from asyncmy import Connection
from asyncmy import (
    connect as _untyped_connect,  # type: ignore[reportUnknownVariableType]
)
from asyncmy.cursors import DictCursor
from asyncmy.errors import Error as AsyncMyError
from langgraph.store.base import Op, Result
from typing_extensions import override

from langgraph.store.mysql.aio_base import BaseAsyncMySQLStore
from langgraph.store.mysql.errors import LangGraphMySQLDriverError


class _ConnectionContext(Protocol):
    """Describe the asyncmy connection context returned by ``connect``."""

    async def __aenter__(self) -> Connection: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


_open_connection = cast(Callable[..., _ConnectionContext], _untyped_connect)


class AsyncMyStore(BaseAsyncMySQLStore[Connection, DictCursor]):
    """Persist LangGraph Store documents through an asyncmy connection.

    ``from_conn_string()`` owns exactly one connection for the duration of its
    asynchronous context. Direct construction borrows the supplied connection and
    leaves its lifecycle with the caller.
    """

    @staticmethod
    def parse_conn_string(conn_string: str) -> dict[str, str | int | None]:
        """Parse a MySQL URL into the keyword arguments accepted by asyncmy.

        Args:
            conn_string: MySQL connection URL. A ``unix_socket`` query parameter is
                forwarded when present.

        Returns:
            Connection arguments with asyncmy defaults for host, port, and password.

        Raises:
            ValueError: The URL contains an invalid port.
        """

        parsed = urllib.parse.urlparse(conn_string)
        if parsed.scheme not in {"mysql", "mysql+asyncmy"}:
            raise ValueError("AsyncMyStore requires a mysql or mysql+asyncmy URL")

        # In order to provide additional params via the connection string,
        # we convert the parsed.query to a dict so we can access the values.
        # This is necessary when using a unix socket, for example.
        params_as_dict = dict(urllib.parse.parse_qsl(parsed.query))

        return {
            "host": parsed.hostname or "localhost",
            "user": (
                None
                if parsed.username is None
                else urllib.parse.unquote(parsed.username)
            ),
            "password": urllib.parse.unquote(parsed.password or ""),
            "db": urllib.parse.unquote(parsed.path[1:]) or None,
            "port": parsed.port or 3306,
            "unix_socket": params_as_dict.get("unix_socket"),
        }

    @classmethod
    @asynccontextmanager
    async def from_conn_string(
        cls,
        conn_string: str,
    ) -> AsyncGenerator[Self, None]:
        """Set up and yield a Store that owns one connection until context exit.

        Schema setup finishes before the Store is exposed. Setup failure, caller
        cancellation, and exceptions raised by the context body all close the owned
        connection through asyncmy's native asynchronous context manager.

        Args:
            conn_string: The MySQL connection info string.

        Yields:
            A ready Store using an autocommit asyncmy connection.

        Raises:
            ValueError: The connection URL is not an accepted MySQL URL.
            LangGraphMySQLDriverError: The owned asyncmy connection lifecycle fails.
            LangGraphMySQLSchemaError: The existing Store table is incompatible.
        """
        connection_context = _open_connection(
            **cls.parse_conn_string(conn_string),
            autocommit=True,
        )
        try:
            conn = await connection_context.__aenter__()
        except AsyncMyError as error:
            raise LangGraphMySQLDriverError(
                operation="open_connection",
                cause=error,
            ) from error

        active_error: BaseException | None = None
        try:
            store = cls(conn=conn)
            await store.setup()
            yield store
        except BaseException as error:
            # The context body owns its failure, including a deliberately raised
            # asyncmy exception. Only errors produced by Store methods are translated
            # at those methods' public boundary.
            active_error = error
            raise
        finally:
            try:
                await connection_context.__aexit__(
                    None if active_error is None else type(active_error),
                    active_error,
                    None if active_error is None else active_error.__traceback__,
                )
            except AsyncMyError as error:
                if active_error is not None:
                    active_error.add_note(
                        "The owned asyncmy connection also failed while closing "
                        f"({type(error).__name__})"
                    )
                else:
                    raise LangGraphMySQLDriverError(
                        operation="close_connection",
                        cause=error,
                    ) from error

    @override
    async def setup(self) -> None:
        """Create and validate the current table through the asyncmy boundary."""

        try:
            await super().setup()
        except AsyncMyError as error:
            raise LangGraphMySQLDriverError(
                operation="setup",
                cause=error,
            ) from error

    @override
    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Execute a Store batch and translate ordinary asyncmy failures."""

        try:
            return await super().abatch(ops)
        except AsyncMyError as error:
            raise LangGraphMySQLDriverError(
                operation="batch",
                cause=error,
            ) from error

    @override
    @staticmethod
    def _get_cursor_from_connection(conn: Connection) -> DictCursor:
        return cast(DictCursor, conn.cursor(DictCursor))
