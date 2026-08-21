"""Stable public failures owned by the TinkerFin runtime."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

_ContextValue: TypeAlias = str | int | float | bool | None


class TinkerFinErrorCode(StrEnum):
    """Stable machine-readable categories for TinkerFin failures."""

    ERROR = "tinkerfin.error"
    LIFECYCLE_ERROR = "tinkerfin.lifecycle_error"
    STREAM_PROTOCOL_ERROR = "tinkerfin.stream_protocol_error"
    PLAN_MODE_CONFIGURATION = "tinkerfin.plan_mode_configuration"
    PLAN_STRUCTURED_OUTPUT = "tinkerfin.plan_structured_output"
    AGUI_NATIVE_STREAM_CONFIGURATION = "tinkerfin.agui_native_stream_configuration"
    AGUI_SETTLEMENT_TIMEOUT = "tinkerfin.agui_settlement_timeout"
    RUN_COORDINATION_FAILED = "tinkerfin.run_coordination_failed"
    RUN_COORDINATION_UNAVAILABLE = "tinkerfin.run_coordination_unavailable"
    RUN_COORDINATION_TIMEOUT = "tinkerfin.run_coordination_timeout"
    RUN_COORDINATION_OWNERSHIP_LOST = "tinkerfin.run_coordination_ownership_lost"
    REDIS_LEASE_FAILED = "tinkerfin.redis_lease_failed"
    REDIS_LEASE_UNAVAILABLE = "tinkerfin.redis_lease_unavailable"
    REDIS_LEASE_TIMEOUT = "tinkerfin.redis_lease_timeout"
    REDIS_LEASE_PROTOCOL_ERROR = "tinkerfin.redis_lease_protocol_error"
    REDIS_LEASE_LIFECYCLE_ERROR = "tinkerfin.redis_lease_lifecycle_error"
    REDIS_LEASE_LOST = "tinkerfin.redis_lease_lost"


class TinkerFinError(Exception):
    """Base failure for framework-owned runtime semantics.

    ``message`` and ``context`` are safe to expose at a client boundary.
    ``diagnostic_context`` and ``cause`` are reserved for trusted logs and
    telemetry and must not be serialized into client responses.

    Args:
        message: Safe human-readable failure summary.
        context: Client-safe machine-readable semantic values.
        diagnostic_context: Implementation details for trusted observability.
        cause: Original failure retained for debugging and exception chaining.
    """

    code: TinkerFinErrorCode = TinkerFinErrorCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, _ContextValue] | None = None,
        diagnostic_context: Mapping[str, _ContextValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
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


class TinkerFinLifecycleError(TinkerFinError, RuntimeError):
    """A runtime object was used outside its valid lifecycle state."""

    code = TinkerFinErrorCode.LIFECYCLE_ERROR


class TinkerFinStreamProtocolError(TinkerFinError, ValueError):
    """A native stream value violates the supported public contract."""

    code = TinkerFinErrorCode.STREAM_PROTOCOL_ERROR


class AgUiNativeStreamConfigurationError(TinkerFinError, ValueError):
    """A native LangGraph stream cannot satisfy AG-UI conversion requirements."""

    code = TinkerFinErrorCode.AGUI_NATIVE_STREAM_CONFIGURATION


class AgUiSettlementTimeoutError(TinkerFinError, TimeoutError):
    """The caller stopped waiting while AG-UI close settlement remains owned."""

    code = TinkerFinErrorCode.AGUI_SETTLEMENT_TIMEOUT

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(
            f"AG-UI settlement timed out after {timeout:g} seconds",
            context={"timeout": timeout},
        )


class RunCoordinationError(TinkerFinError, RuntimeError):
    """A replaceable run coordinator failed at its framework boundary."""

    code = TinkerFinErrorCode.RUN_COORDINATION_FAILED


class RunCoordinationUnavailableError(RunCoordinationError):
    """The configured run coordinator is temporarily unavailable."""

    code = TinkerFinErrorCode.RUN_COORDINATION_UNAVAILABLE


class RunCoordinationTimeoutError(RunCoordinationError):
    """A run coordination operation exceeded its bounded wait."""

    code = TinkerFinErrorCode.RUN_COORDINATION_TIMEOUT


class RunCoordinationOwnershipLostError(RunCoordinationError):
    """A coordinated run can no longer authorize protected work."""

    code = TinkerFinErrorCode.RUN_COORDINATION_OWNERSHIP_LOST


class RedisLeaseError(TinkerFinError, RuntimeError):
    """Base failure for the optional Redis lease implementation."""

    code = TinkerFinErrorCode.REDIS_LEASE_FAILED


class RedisLeaseUnavailableError(RedisLeaseError):
    """Redis is unavailable for a lease operation."""

    code = TinkerFinErrorCode.REDIS_LEASE_UNAVAILABLE


class RedisLeaseTimeoutError(RedisLeaseError):
    """A Redis lease command exceeded its bounded wait."""

    code = TinkerFinErrorCode.REDIS_LEASE_TIMEOUT


class RedisLeaseProtocolError(RedisLeaseError):
    """Redis returned a value outside the lease protocol contract."""

    code = TinkerFinErrorCode.REDIS_LEASE_PROTOCOL_ERROR


class RedisLeaseLifecycleError(RedisLeaseError):
    """A Redis lease object was used outside its valid lifecycle state."""

    code = TinkerFinErrorCode.REDIS_LEASE_LIFECYCLE_ERROR


__all__ = [
    "AgUiNativeStreamConfigurationError",
    "AgUiSettlementTimeoutError",
    "RedisLeaseError",
    "RedisLeaseLifecycleError",
    "RedisLeaseProtocolError",
    "RedisLeaseTimeoutError",
    "RedisLeaseUnavailableError",
    "RunCoordinationError",
    "RunCoordinationOwnershipLostError",
    "RunCoordinationTimeoutError",
    "RunCoordinationUnavailableError",
    "TinkerFinError",
    "TinkerFinErrorCode",
    "TinkerFinLifecycleError",
    "TinkerFinStreamProtocolError",
]
