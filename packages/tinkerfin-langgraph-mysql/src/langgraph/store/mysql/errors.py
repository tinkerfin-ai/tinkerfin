"""Stable failures owned by the asyncmy LangGraph MySQL implementation."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class LangGraphMySQLErrorCode(StrEnum):
    """Stable machine-readable categories for integration failures."""

    ERROR = "tinkerfin_langgraph_mysql.error"
    DRIVER_FAILURE = "tinkerfin_langgraph_mysql.driver_failure"
    SCHEMA_MISMATCH = "tinkerfin_langgraph_mysql.schema_mismatch"


class LangGraphMySQLError(Exception):
    """Base failure with separate public and trusted diagnostic context.

    Args:
        message: Client-safe failure summary.
        context: Client-safe semantic values.
        diagnostic_context: Implementation evidence reserved for trusted logs.
        cause: Original failure retained for diagnostics and exception chaining.
    """

    code: LangGraphMySQLErrorCode = LangGraphMySQLErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Snapshot safe context and retain the original diagnostic failure."""

        self.message = message
        self.context: Mapping[str, _ContextValue] = MappingProxyType(
            dict(context or {})
        )
        self.diagnostic_context: Mapping[str, _ContextValue] = MappingProxyType(
            dict(diagnostic_context or {})
        )
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)


class LangGraphMySQLDriverError(LangGraphMySQLError, RuntimeError):
    """The asyncmy boundary failed while opening, using, or closing a connection."""

    code = LangGraphMySQLErrorCode.DRIVER_FAILURE

    def __init__(self, *, operation: str, cause: BaseException) -> None:
        """Record a stable operation without exposing vendor error text."""

        super().__init__(
            "The LangGraph MySQL Store driver failed",
            context={"operation": operation},
            diagnostic_context={"cause_type": type(cause).__name__},
            cause=cause,
        )


class LangGraphMySQLSchemaError(LangGraphMySQLError, RuntimeError):
    """The existing Store table does not match the only current Schema."""

    code = LangGraphMySQLErrorCode.SCHEMA_MISMATCH

    def __init__(self, *, reason: str) -> None:
        """Retain the non-sensitive mismatch reason for trusted diagnostics."""

        super().__init__(
            "The LangGraph MySQL Store schema does not match the current contract",
            context={"table": "store"},
            diagnostic_context={"reason": reason},
        )


__all__ = [
    "LangGraphMySQLDriverError",
    "LangGraphMySQLError",
    "LangGraphMySQLErrorCode",
    "LangGraphMySQLSchemaError",
]
