"""Replayable messaging with TinkerFin Native and AG-UI integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from .backend import BackendRunHandle as BackendRunHandle
from .backend import MemoryBackend as MemoryBackend
from .backend import MessagingBackend as MessagingBackend
from .backend import PreparedRun as PreparedRun
from .errors import BackendOwnershipLost as BackendOwnershipLost
from .errors import CancellationUnsupported as CancellationUnsupported
from .errors import CodecMismatch as CodecMismatch
from .errors import InvalidCursor as InvalidCursor
from .errors import MessageIdConflict as MessageIdConflict
from .errors import MessagingBackendError as MessagingBackendError
from .errors import MessagingBackendProtocolError as MessagingBackendProtocolError
from .errors import MessagingBackendTimeout as MessagingBackendTimeout
from .errors import MessagingBackendUnavailable as MessagingBackendUnavailable
from .errors import MessagingClosed as MessagingClosed
from .errors import MessagingError as MessagingError
from .errors import MessagingErrorCode as MessagingErrorCode
from .errors import MessagingNotStarted as MessagingNotStarted
from .errors import MessagingQuotaExceeded as MessagingQuotaExceeded
from .errors import MessagingSettlementTimeout as MessagingSettlementTimeout
from .errors import RecoveryUnsupported as RecoveryUnsupported
from .errors import RunAlreadyActive as RunAlreadyActive
from .errors import RunNotFound as RunNotFound
from .errors import RunProducerFailed as RunProducerFailed
from .errors import SourceProfileMismatch as SourceProfileMismatch
from .errors import SseRenderingUnsupported as SseRenderingUnsupported
from .errors import StreamDeleteConflict as StreamDeleteConflict
from .errors import StreamDeleted as StreamDeleted
from .errors import UnexpectedMessagingBackendError as UnexpectedMessagingBackendError
from .limits import MessagingLimits as MessagingLimits
from .messaging import CancelCallback as CancelCallback
from .messaging import CancelContext as CancelContext
from .messaging import CommittedCallback as CommittedCallback
from .messaging import MessageChannel as MessageChannel
from .messaging import MessageSubscription as MessageSubscription
from .messaging import Messaging as Messaging
from .models import DecodedMessage as DecodedMessage
from .models import MessageEnvelope as MessageEnvelope
from .models import RecoverableMessage as RecoverableMessage
from .models import RecoveryCheckpoint as RecoveryCheckpoint
from .protocols import MessageCodec as MessageCodec
from .protocols import MessageSource as MessageSource
from .protocols import ProfiledMessageSource as ProfiledMessageSource
from .protocols import RecoverableSource as RecoverableSource
from .protocols import SseRenderer as SseRenderer
from .sources import CancellableMessageSource as CancellableMessageSource
from .sources import DeferredMessageSource as DeferredMessageSource
from .sources import FiniteMessageSource as FiniteMessageSource
from .sources import MessageSourceBinding as MessageSourceBinding
from .sources import ProfiledDeferredMessageSource as ProfiledDeferredMessageSource
from .sources import map_source as map_source

if TYPE_CHECKING:
    from .agui import AgUiCodec as AgUiCodec
    from .native import NativeStreamPart as NativeStreamPart
    from .native import NativeStreamPartCodec as NativeStreamPartCodec
    from .redis import RedisBackend as RedisBackend

__all__ = [
    "AgUiCodec",
    "BackendOwnershipLost",
    "BackendRunHandle",
    "CancelCallback",
    "CancelContext",
    "CancellableMessageSource",
    "CancellationUnsupported",
    "CodecMismatch",
    "CommittedCallback",
    "DecodedMessage",
    "DeferredMessageSource",
    "FiniteMessageSource",
    "InvalidCursor",
    "MemoryBackend",
    "MessageChannel",
    "MessageCodec",
    "MessageEnvelope",
    "MessageIdConflict",
    "MessageSource",
    "MessageSourceBinding",
    "MessageSubscription",
    "Messaging",
    "MessagingBackend",
    "MessagingBackendError",
    "MessagingBackendProtocolError",
    "MessagingBackendTimeout",
    "MessagingBackendUnavailable",
    "MessagingClosed",
    "MessagingError",
    "MessagingErrorCode",
    "MessagingLimits",
    "MessagingNotStarted",
    "MessagingQuotaExceeded",
    "MessagingSettlementTimeout",
    "NativeStreamPart",
    "NativeStreamPartCodec",
    "PreparedRun",
    "ProfiledDeferredMessageSource",
    "ProfiledMessageSource",
    "RecoverableMessage",
    "RecoverableSource",
    "RecoveryCheckpoint",
    "RecoveryUnsupported",
    "RedisBackend",
    "RunAlreadyActive",
    "RunNotFound",
    "RunProducerFailed",
    "SourceProfileMismatch",
    "SseRenderer",
    "SseRenderingUnsupported",
    "StreamDeleteConflict",
    "StreamDeleted",
    "UnexpectedMessagingBackendError",
    "map_source",
]


def _raise_missing_extra(
    error: ModuleNotFoundError,
    *,
    symbol: str,
    extra: str,
    packages: tuple[str, ...],
) -> Never:
    missing = error.name
    if missing is None or not any(
        missing == package or missing.startswith(f"{package}.") for package in packages
    ):
        raise error
    raise ImportError(
        f"{symbol} requires optional dependencies from the {extra!r} extra; "
        f'install them with: pip install "tinkerfin-messaging[{extra}]"'
    ) from error


def __getattr__(name: str) -> object:
    """Load optional integrations only when their public symbol is requested."""

    if name == "AgUiCodec":
        from .agui import AgUiCodec

        globals()[name] = AgUiCodec
        return AgUiCodec
    if name in {"NativeStreamPart", "NativeStreamPartCodec"}:
        from .native import NativeStreamPart, NativeStreamPartCodec

        globals()["NativeStreamPart"] = NativeStreamPart
        globals()["NativeStreamPartCodec"] = NativeStreamPartCodec
        return globals()[name]
    if name == "RedisBackend":
        try:
            from .redis import RedisBackend
        except ModuleNotFoundError as error:
            _raise_missing_extra(
                error,
                symbol=name,
                extra="redis",
                packages=("redis",),
            )

        globals()[name] = RedisBackend
        return RedisBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
