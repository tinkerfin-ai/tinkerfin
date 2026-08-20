"""Protocol-neutral producer, replay subscription, and SSE facade."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
)
from dataclasses import dataclass, field
from types import TracebackType
from typing import Generic, Literal, Never, Self, TypeAlias, TypeVar, cast, overload

from tinkerfin_agui_adapter import Identity

from ._identity import required_identifier, required_identity
from .backend import BackendRunHandle, MemoryBackend, MessagingBackend, PreparedRun
from .errors import (
    BackendOwnershipLost,
    CodecMismatch,
    InvalidCursor,
    MessagingBackendError,
    MessagingClosed,
    MessagingError,
    MessagingNotStarted,
    MessagingSettlementTimeout,
    RunProducerFailed,
    SourceProfileMismatch,
    SseRenderingUnsupported,
    UnexpectedMessagingBackendError,
)
from .models import (
    DecodedMessage,
    MessageEnvelope,
    RecoverableMessage,
    RecoveryCheckpoint,
)
from .protocols import (
    MessageCodec,
    MessageSource,
    ProfiledMessageSource,
    RecoverableSource,
    SseRenderer,
)

SourceT = TypeVar("SourceT")
ReplayT = TypeVar("ReplayT")
ProducedT = TypeVar("ProducedT")
ProfileSourceT = TypeVar("ProfileSourceT")
ProfileReplayT = TypeVar("ProfileReplayT")
BackendResultT = TypeVar("BackendResultT")

logger = logging.getLogger(__name__)

_ProducerFailureStage: TypeAlias = Literal[
    "callback",
    "commit",
    "finish",
    "lease",
    "settlement",
    "shutdown",
    "source",
    "source_close",
    "tail",
]


def _unexpected_backend_error(
    operation: str,
    error: Exception,
) -> UnexpectedMessagingBackendError:
    return UnexpectedMessagingBackendError(
        f"Messaging backend {operation} failed",
        diagnostic_context={"operation": operation},
        cause=error,
    )


async def _await_backend(
    operation: str,
    awaitable: Awaitable[BackendResultT],
) -> BackendResultT:
    try:
        return await awaitable
    except MessagingError as error:
        if isinstance(error, MessagingBackendError):
            error._enrich_diagnostic_context({"operation": operation})
        raise
    except Exception as error:
        translated = _unexpected_backend_error(operation, error)
        raise translated from error


def _read_backend(
    operation: str,
    reader: Callable[[], BackendResultT],
) -> BackendResultT:
    try:
        return reader()
    except MessagingError as error:
        if isinstance(error, MessagingBackendError):
            error._enrich_diagnostic_context({"operation": operation})
        raise
    except Exception as error:
        translated = _unexpected_backend_error(operation, error)
        raise translated from error


async def _iterate_backend(
    operation: str,
    iterator: AsyncIterator[BackendResultT],
) -> AsyncGenerator[BackendResultT, None]:
    try:
        async for item in iterator:
            yield item
    except MessagingError as error:
        if isinstance(error, MessagingBackendError):
            error._enrich_diagnostic_context({"operation": operation})
        raise
    except Exception as error:
        translated = _unexpected_backend_error(operation, error)
        raise translated from error


def _derived_message_id(identity: Identity, ordinal: int) -> str:
    """Keep ordinary message IDs bounded without changing the common readable form."""

    candidate = f"{identity.run_id}:{ordinal}"
    if len(candidate) <= 1024:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()
    return f"sha256:{digest}"


def _validate_optional_cursor(after: int | None) -> None:
    if after is not None and (isinstance(after, bool) or not isinstance(after, int)):
        raise TypeError("after must be an integer or None")


@dataclass(frozen=True, slots=True)
class CancelContext:
    """Identify the run whose accepted cancellation invokes a callback."""

    channel: str
    identity: Identity

    def __post_init__(self) -> None:
        required_identifier("channel", self.channel)
        required_identity(self.identity)


_CancelResult: TypeAlias = (
    Iterable[ProducedT] | Awaitable[Iterable[ProducedT] | None] | None
)
_ContextCancelCallback: TypeAlias = Callable[
    [CancelContext],
    _CancelResult[ProducedT],
]
CancelCallback: TypeAlias = (
    Callable[[], _CancelResult[ProducedT]] | _ContextCancelCallback[ProducedT]
)
CommittedCallback: TypeAlias = Callable[[MessageEnvelope], Awaitable[None]]


@dataclass(slots=True)
class _ProducerState:
    cancel_requested: bool = False
    settling: bool = False
    ownership_lost: bool = False
    ownership_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _ProducedMessage(Generic[SourceT]):
    data: SourceT
    message_id: str
    checkpoint: RecoveryCheckpoint | None


@dataclass(slots=True)
class _PendingCommit(Generic[ProducedT]):
    item: ProducedT
    acknowledged: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None


async def _close_async_iterator(iterator: AsyncIterator[object]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _invoke_cancel(
    callback: _ContextCancelCallback[ProducedT],
    context: CancelContext,
) -> Iterable[ProducedT] | None:
    result = callback(context)
    if inspect.isawaitable(result):
        return await cast(Awaitable[Iterable[ProducedT] | None], result)
    return result


def _normalize_cancel_callback(
    callback: CancelCallback[ProducedT],
) -> _ContextCancelCallback[ProducedT]:
    """Normalize one supported public signature before claiming a durable run."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError) as error:
        raise TypeError(
            "cancel callback must expose an inspectable signature"
        ) from error

    try:
        signature.bind()
    except TypeError:
        pass
    else:
        zero_argument = cast(Callable[[], _CancelResult[ProducedT]], callback)

        def invoke_without_context(
            _context: CancelContext,
        ) -> _CancelResult[ProducedT]:
            return zero_argument()

        return invoke_without_context

    try:
        signature.bind(
            CancelContext(
                channel="callback",
                identity=Identity(threadId="callback", runId="callback"),
            )
        )
    except TypeError as error:
        raise TypeError(
            "cancel callback must accept no arguments or one positional CancelContext"
        ) from error
    return cast(_ContextCancelCallback[ProducedT], callback)


async def _join_owned_task(task: asyncio.Task[None]) -> None:
    """Settle an owned task while preserving caller cancellation and task failure."""

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            next_cancel_count = current.cancelling() if current is not None else 0
            if next_cancel_count > cancel_count:
                if caller_cancellation is None:
                    caller_cancellation = cancellation
                cancel_count = next_cancel_count
                continue
            if task.done():
                break
            raise
        except BaseException:
            if task.done():
                break
            raise

    task_error: BaseException | None = None
    try:
        task.result()
    except BaseException as error:  # noqa: BLE001 - preserve owned task outcome
        task_error = error
    if caller_cancellation is not None:
        if task_error is not None:
            caller_cancellation.add_note(
                "Messaging close also failed: "
                f"{type(task_error).__name__}: {task_error}"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)


class MessageSubscription(Generic[ReplayT]):
    """Decode one run-bounded replay and close its backend iterator exactly once."""

    def __init__(
        self,
        *,
        backend: MessagingBackend,
        prepared: PreparedRun,
        codec: MessageCodec[object, ReplayT],
        renderer: SseRenderer[ReplayT] | None,
    ) -> None:
        self._backend = backend
        self._prepared = prepared
        self._codec = codec
        self._renderer = renderer
        self._claimed = False
        self._delivery: AsyncIterator[DecodedMessage[ReplayT]] | None = None
        self._backend_iterator: AsyncIterator[object] | None = None

    def __aiter__(self) -> AsyncIterator[DecodedMessage[ReplayT]]:
        if self._claimed:
            raise RuntimeError("a message subscription can only be consumed once")
        self._claimed = True
        delivery = self._iterate()
        self._delivery = delivery
        return delivery

    async def _iterate(self) -> AsyncGenerator[DecodedMessage[ReplayT], None]:
        backend_iterator = _read_backend(
            "follow",
            lambda: self._backend.follow(
                self._prepared.handle,
                after=self._prepared.after,
            ),
        )
        self._backend_iterator = cast(AsyncIterator[object], backend_iterator)
        primary: BaseException | None = None
        try:
            async for envelope in _iterate_backend("follow", backend_iterator):
                expected_codec = self._codec.codec_id
                if envelope.codec != expected_codec:
                    raise CodecMismatch(
                        expected=expected_codec,
                        actual=envelope.codec,
                    )
                yield DecodedMessage(
                    envelope=envelope,
                    data=self._codec.decode(envelope.payload),
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await _await_backend(
                    "follow close",
                    _close_async_iterator(
                        cast(AsyncIterator[object], backend_iterator)
                    ),
                )
            except BaseException as close_error:
                if primary is None:
                    raise
                primary.add_note(f"Messaging follow cleanup also failed: {close_error}")
            finally:
                self._backend_iterator = None

    def sse(self) -> AsyncIterator[bytes]:
        """Render committed messages while preserving their durable sequences."""

        renderer = self._renderer
        if renderer is None:
            raise SseRenderingUnsupported("This channel has no SSE renderer")

        async def iterate() -> AsyncGenerator[bytes, None]:
            try:
                async for message in self:
                    yield renderer.render(
                        seq=message.envelope.seq,
                        payload=message.data,
                    )
            finally:
                await self.aclose()

        return iterate()

    async def aclose(self) -> None:
        """Detach this subscriber without affecting the producer."""

        delivery = self._delivery
        if delivery is not None:
            await _close_async_iterator(cast(AsyncIterator[object], delivery))
            self._delivery = None
            return
        backend_iterator = self._backend_iterator
        if backend_iterator is not None:
            await _await_backend(
                "follow close",
                _close_async_iterator(backend_iterator),
            )
            self._backend_iterator = None


class MessageChannel(Generic[SourceT, ReplayT]):
    """Bind one stable codec to shared framework run identities."""

    def __init__(
        self,
        *,
        messaging: Messaging,
        name: str,
        codec: MessageCodec[SourceT, ReplayT] | None,
        renderer: SseRenderer[ReplayT] | None,
    ) -> None:
        self._messaging = messaging
        self.name = required_identifier("channel name", name)
        if codec is None and renderer is not None:
            raise TypeError("renderer requires an explicit codec")
        codec_id = (
            None
            if codec is None
            else required_identifier(
                "codec_id",
                codec.codec_id,
            )
        )
        self._codec_id = codec_id
        self._codec = codec
        self._renderer = renderer
        self._inferred_profile: str | None = None

    @staticmethod
    def _resolve_identity(source: object, identity: Identity | None) -> Identity:
        """Resolve one explicit or immutable source identity before side effects."""

        explicit = None if identity is None else required_identity(identity)
        source_identity = getattr(source, "messaging_identity", None)
        profiled = (
            None if source_identity is None else required_identity(source_identity)
        )
        if profiled is None:
            if explicit is None:
                raise TypeError(
                    "source does not advertise messaging_identity; provide "
                    "identity explicitly"
                )
            return explicit
        if explicit is not None and explicit != profiled:
            raise ValueError("explicit identity must match source messaging_identity")
        return profiled

    def _resolve_binding(
        self,
        source: object,
    ) -> tuple[
        MessageCodec[SourceT, ReplayT],
        SseRenderer[ReplayT] | None,
        str | None,
    ]:
        """Resolve an explicit codec or one supported structural source profile."""

        if bool(getattr(source, "is_tinkerfin_sse_body", False)):
            raise TypeError(
                "Messaging requires object events, not pre-encoded SSE frames"
            )
        codec = self._codec
        inferred_profile = self._inferred_profile
        if codec is not None and inferred_profile is None:
            return codec, self._renderer, None
        profile = getattr(source, "messaging_codec_profile", None)
        if profile is None:
            if inferred_profile is not None:
                raise TypeError(
                    "source does not advertise the channel's inferred codec profile"
                )
            raise TypeError(
                "source does not advertise a built-in codec; provide codec explicitly"
            )
        if not isinstance(profile, str):
            raise TypeError("source messaging_codec_profile must be a string")
        if inferred_profile is not None:
            if profile != inferred_profile:
                raise CodecMismatch(expected=inferred_profile, actual=profile)
            assert codec is not None
            self._validate_profile_types(source=source, profile=profile, codec=codec)
            return codec, self._renderer, profile
        if profile == "agui.event.v1":
            from .agui import AgUiCodec

            built_in = AgUiCodec()
        elif profile == "langgraph.stream-part.v2.v1":
            from .native import NativeStreamPartCodec

            built_in = NativeStreamPartCodec()
        else:
            raise TypeError(f"unsupported built-in codec profile: {profile!r}")
        self._validate_profile_types(source=source, profile=profile, codec=built_in)
        current = self._inferred_profile
        if current is not None and current != profile:
            raise CodecMismatch(expected=current, actual=profile)
        return (
            cast(MessageCodec[SourceT, ReplayT], built_in),
            cast(SseRenderer[ReplayT], built_in),
            profile,
        )

    @staticmethod
    def _validate_profile_types(
        *,
        source: object,
        profile: str,
        codec: object,
    ) -> None:
        """Prove declared live and replay types before durable preparation."""

        for attribute, label in (
            ("messaging_source_type", "live source type"),
            ("messaging_replay_type", "replay type"),
        ):
            missing = object()
            declared = getattr(source, attribute, missing)
            if declared is missing:
                raise SourceProfileMismatch(
                    profile=profile,
                    reason=f"missing {label} metadata",
                )
            expected = getattr(codec, attribute, missing)
            if expected is missing:
                raise SourceProfileMismatch(
                    profile=profile,
                    reason=f"built-in codec is missing {label} metadata",
                )
            if declared is not expected:
                expected_name = getattr(
                    expected, "__qualname__", type(expected).__name__
                )
                raise SourceProfileMismatch(
                    profile=profile,
                    reason=f"{label} metadata must be {expected_name}",
                )

    def _commit_inferred_binding(
        self,
        *,
        codec: MessageCodec[SourceT, ReplayT],
        renderer: SseRenderer[ReplayT] | None,
        profile: str | None,
    ) -> None:
        if profile is None or self._codec is not None:
            return
        current = self._inferred_profile
        if current is not None and current != profile:
            raise CodecMismatch(expected=current, actual=profile)
        self._codec = codec
        self._codec_id = required_identifier("codec_id", codec.codec_id)
        self._renderer = renderer
        self._inferred_profile = profile

    def _require_read_codec(self) -> MessageCodec[SourceT, ReplayT]:
        codec = self._codec
        if codec is None:
            raise TypeError(
                "channel codec is unknown; provide one explicitly or infer it from "
                "a profiled source first"
            )
        return codec

    @staticmethod
    def _validate_page(*, after: int, limit: int | None = None) -> None:
        if isinstance(after, bool) or not isinstance(after, int):
            raise TypeError("after must be an integer")
        if after < 0:
            raise ValueError("after must be greater than or equal to zero")
        if limit is None:
            return
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

    async def latest_seq(self, *, identity: Identity) -> int:
        """Return the greatest committed sequence, or zero for an empty stream."""

        preflight = self._messaging._begin_preflight()
        try:
            required_identity(identity)
            self._messaging._require_open()
            latest = await _await_backend(
                "latest_seq",
                self._messaging.backend.latest_seq(
                    channel=self.name,
                    identity=identity,
                ),
            )
            self._messaging._require_open()
            return latest
        finally:
            self._messaging._finish_preflight(preflight)

    async def read(
        self,
        *,
        identity: Identity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[DecodedMessage[ReplayT], ...]:
        """Decode one ascending committed page after an exclusive cursor."""

        self._validate_page(after=after, limit=limit)
        preflight = self._messaging._begin_preflight()
        try:
            required_identity(identity)
            codec = self._require_read_codec()
            expected_codec = required_identifier("codec_id", codec.codec_id)
            self._messaging._require_open()
            envelopes = await _await_backend(
                "read",
                self._messaging.backend.read(
                    channel=self.name,
                    identity=identity,
                    after=after,
                    limit=limit,
                ),
            )
            decoded: list[DecodedMessage[ReplayT]] = []
            for envelope in envelopes:
                if envelope.codec != expected_codec:
                    raise CodecMismatch(
                        expected=expected_codec,
                        actual=envelope.codec,
                    )
                decoded.append(
                    DecodedMessage(
                        envelope=envelope,
                        data=codec.decode(envelope.payload),
                    )
                )
            self._messaging._require_open()
            return tuple(decoded)
        finally:
            self._messaging._finish_preflight(preflight)

    async def follow(
        self,
        *,
        identity: Identity,
        after: int = 0,
    ) -> MessageSubscription[ReplayT]:
        """Follow one run's committed events through its authoritative terminal."""

        preflight = self._messaging._begin_preflight()
        try:
            self._validate_page(after=after)
            required_identity(identity)
            codec = self._require_read_codec()
            self._messaging._require_open()
            handle = await _await_backend(
                "bind_follow",
                self._messaging.backend.bind_follow(
                    channel=self.name,
                    identity=identity,
                ),
            )
            self._messaging._require_open()
            return MessageSubscription(
                backend=self._messaging.backend,
                prepared=PreparedRun(handle=handle, after=after, is_owner=False),
                codec=cast(MessageCodec[object, ReplayT], codec),
                renderer=self._renderer,
            )
        finally:
            self._messaging._finish_preflight(preflight)

    async def validate_cursor(
        self,
        *,
        identity: Identity,
        after: int | None,
    ) -> None:
        """Validate one replay cursor without creating or attaching a run.

        This read-only preflight lets a host reject an out-of-range reconnect cursor
        before it opens an application source. `wrap()` still repeats cursor
        validation atomically with its start-or-attach decision.

        Args:
            identity: Run identity whose thread-level committed tail is checked.
            after: Exclusive replay cursor, or `None` to start at the current tail.

        Raises:
            InvalidCursor: `after` falls outside the current committed range.
            MessagingError: Messaging closes while the backend lookup is in flight.
            TypeError: `after` is neither an integer nor `None`.
            ValueError: `identity` contains a non-canonical identifier.
        """

        preflight = self._messaging._begin_preflight()
        try:
            required_identity(identity)
            if after is None:
                return
            if isinstance(after, bool) or not isinstance(after, int):
                raise TypeError("after must be an integer or None")
            latest = await _await_backend(
                "latest_seq",
                self._messaging.backend.latest_seq(
                    channel=self.name,
                    identity=identity,
                ),
            )
            self._messaging._require_open()
            if after < 0 or after > latest:
                raise InvalidCursor(after=after, latest=latest)
        finally:
            self._messaging._finish_preflight(preflight)

    @overload
    async def wrap(
        self,
        source: MessageSource[SourceT],
        *,
        identity: Identity,
        after: int | None = None,
        cancel: CancelCallback[SourceT] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> MessageSubscription[ReplayT]: ...

    @overload
    async def wrap(
        self,
        source: ProfiledMessageSource[ProfileSourceT, ProfileReplayT],
        *,
        identity: Identity | None = None,
        after: int | None = None,
        cancel: CancelCallback[ProfileSourceT] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> MessageSubscription[ProfileReplayT]: ...

    async def wrap(
        self,
        source: (
            MessageSource[SourceT]
            | ProfiledMessageSource[ProfileSourceT, ProfileReplayT]
        ),
        *,
        identity: Identity | None = None,
        after: int | None = None,
        cancel: (
            CancelCallback[SourceT] | CancelCallback[ProfileSourceT] | None
        ) = None,
        on_committed: CommittedCallback | None = None,
    ) -> MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT]:
        return cast(
            MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT],
            await self._wrap(
                cast(MessageSource[object], source),
                identity=identity,
                after=after,
                cancel=cast(CancelCallback[object] | None, cancel),
                on_committed=on_committed,
            ),
        )

    async def _wrap(
        self,
        source: MessageSource[object],
        *,
        identity: Identity | None = None,
        after: int | None = None,
        cancel: CancelCallback[object] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> MessageSubscription[object]:
        """Validate and start-or-attach before an HTTP response is constructed.

        Args:
            source: Single-use source owned and eventually closed by Messaging.
            identity: Explicit run identity for a custom source, or an optional
                equality check for a profiled TinkerFin source.
            after: Exclusive durable replay cursor, or the current tail when omitted.
            cancel: At-most-once synchronous or asynchronous callback. Omit it when the
                source declares `messaging_cancel_callback`. Supplying the same owner
                is accepted; a different callback is an ownership error. A callback
                may return `None` or a finite iterable of
                additional source values, which are encoded and appended after already
                accepted source values. It may accept no arguments or one required
                positional `CancelContext`, and is responsible for stopping the active
                source. A signature that also accepts no arguments is invoked without
                the context.
            on_committed: Asynchronous owner-only observer invoked with each durable
                envelope after append. Observer failures are logged and do not change
                the run outcome; replay attachments do not invoke it.

        Returns:
            A detachable subscription over committed decoded messages.

        Raises:
            MessagingError: Backend preflight or producer startup is rejected.
            TypeError: `cancel` does not expose one of the supported signatures.
            ValueError: The explicit and source identities conflict.
        """

        preflight = self._messaging._begin_preflight()
        prepared: PreparedRun | None = None
        normalized_cancel: _ContextCancelCallback[object] | None = None
        producer_started = False
        source_released = False
        try:
            _validate_optional_cursor(after)
            codec, renderer, profile = self._resolve_binding(source)
            producer_codec = cast(MessageCodec[object, object], codec)
            replay_renderer = cast(SseRenderer[object] | None, renderer)
            codec_id = required_identifier("codec_id", codec.codec_id)
            resolved_identity = self._resolve_identity(source, identity)
            source_cancel = getattr(source, "messaging_cancel_callback", None)
            if source_cancel is not None:
                if cancel is not None:
                    matcher = getattr(
                        source,
                        "messaging_cancel_callback_matches",
                        None,
                    )
                    equivalent = cancel == source_cancel or (
                        callable(matcher) and bool(matcher(cancel))
                    )
                    if not equivalent:
                        raise TypeError(
                            "cancel must be omitted when the source owns cancellation"
                        )
                cancel = cast(CancelCallback[object], source_cancel)
            if cancel is not None:
                normalized_cancel = _normalize_cancel_callback(cancel)
            prepared = await _await_backend(
                "prepare",
                self._messaging.backend.prepare(
                    channel=self.name,
                    identity=resolved_identity,
                    codec=codec_id,
                    after=after,
                    cancellable=normalized_cancel is not None,
                    recoverable=False,
                ),
            )
            self._messaging._require_open()
            self._commit_inferred_binding(
                codec=codec,
                renderer=renderer,
                profile=profile,
            )
            if prepared.is_owner:
                started = self._messaging._start_producer(
                    prepared=prepared,
                    source=source,
                    codec=producer_codec,
                    cancel=normalized_cancel,
                    on_committed=on_committed,
                )
                producer_started = True
                await started.wait()
                self._messaging._require_open()
            else:
                await source.aclose()
                source_released = True
                self._messaging._require_open()
        except BaseException as error:
            if not producer_started and not source_released:
                try:
                    await source.aclose()
                finally:
                    if prepared is not None and prepared.is_owner:
                        try:
                            await _await_backend(
                                "finish",
                                self._messaging.backend.finish(
                                    prepared.handle,
                                    status="failed",
                                    error=error,
                                ),
                            )
                        except BackendOwnershipLost:
                            pass
            raise
        finally:
            self._messaging._finish_preflight(preflight)

        return MessageSubscription(
            backend=self._messaging.backend,
            prepared=prepared,
            codec=producer_codec,
            renderer=replay_renderer,
        )

    @overload
    async def sse(
        self,
        source: MessageSource[SourceT],
        *,
        identity: Identity,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[SourceT] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> AsyncIterator[bytes]: ...

    @overload
    async def sse(
        self,
        source: ProfiledMessageSource[ProfileSourceT, ProfileReplayT],
        *,
        identity: Identity | None = None,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[ProfileSourceT] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def sse(
        self,
        source: MessageSource[object],
        *,
        identity: Identity | None = None,
        after: int | Callable[[], int | None] | None = None,
        cancel: CancelCallback[object] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> AsyncIterator[bytes]:
        """Prepare durable publication and return its SSE response body.

        Args:
            source: Single-use object source owned and eventually closed by Messaging.
            identity: Explicit run identity for a custom source, or an optional
                equality check for a profiled TinkerFin source.
            after: Exclusive replay cursor, a zero-argument synchronous resolver
                returning one, or `None` to start at the current tail. A resolver is
                invoked exactly once before durable preparation.
            cancel: Optional callback used for accepted remote cancellation. Omit it
                when the source declares its own callback.
            on_committed: Optional owner-only observer for newly committed envelopes.

        Returns:
            A durable SSE body whose IDs are committed channel sequence numbers.

        Raises:
            MessagingError: Durable preparation or producer startup is rejected.
            TypeError: The resolved cursor is neither an integer nor `None`.
            ValueError: The explicit and source identities conflict.
        """

        try:
            resolved_after = after() if callable(after) else after
        except BaseException:
            await source.aclose()
            raise

        subscription = await self._wrap(
            source,
            identity=identity,
            after=resolved_after,
            cancel=cancel,
            on_committed=on_committed,
        )
        try:
            return subscription.sse()
        except BaseException:
            await subscription.aclose()
            raise

    async def wrap_recoverable(
        self,
        source: RecoverableSource[SourceT],
        *,
        identity: Identity | None = None,
        after: int | None = None,
        cancel: CancelCallback[RecoverableMessage[SourceT]] | None = None,
        on_committed: CommittedCallback | None = None,
    ) -> MessageSubscription[ReplayT]:
        """Start or rebuild an owner from its last committed checkpoint.

        Args:
            source: Factory that rebuilds one owned source from a checkpoint.
            identity: Explicit run identity for a custom source, or an optional
                equality check for a profiled source factory.
            after: Exclusive durable replay cursor, or the current tail when omitted.
            cancel: At-most-once synchronous or asynchronous callback. Returned tail
                values must be `RecoverableMessage` instances with stable IDs and
                matching checkpoints. The callback is responsible for causing the
                active source to stop. It may accept no arguments or one required
                positional `CancelContext`; a signature that also accepts no
                arguments is invoked without the context.
            on_committed: Asynchronous owner-only observer invoked after every
                successful append, including idempotent recovery commits.

        Returns:
            A detachable subscription over committed decoded messages.

        Raises:
            MessagingError: Backend preflight, recovery, or startup is rejected.
            TypeError: `cancel` does not expose one of the supported signatures.
            ValueError: The explicit and source identities conflict.
        """

        preflight = self._messaging._begin_preflight()
        prepared: PreparedRun | None = None
        opened: MessageSource[RecoverableMessage[SourceT]] | None = None
        normalized_cancel: (
            _ContextCancelCallback[RecoverableMessage[SourceT]] | None
        ) = None
        producer_started = False
        try:
            _validate_optional_cursor(after)
            codec, renderer, profile = self._resolve_binding(source)
            codec_id = required_identifier("codec_id", codec.codec_id)
            resolved_identity = self._resolve_identity(source, identity)
            if cancel is not None:
                normalized_cancel = _normalize_cancel_callback(cancel)
            prepared = await _await_backend(
                "prepare",
                self._messaging.backend.prepare(
                    channel=self.name,
                    identity=resolved_identity,
                    codec=codec_id,
                    after=after,
                    cancellable=normalized_cancel is not None,
                    recoverable=True,
                ),
            )
            self._messaging._require_open()
            self._commit_inferred_binding(
                codec=codec,
                renderer=renderer,
                profile=profile,
            )
            if prepared.is_owner:
                opened = await self._messaging._open_recoverable_source(
                    prepared=prepared,
                    source=source,
                )
                self._messaging._require_open()
                started = self._messaging._start_recoverable_producer(
                    prepared=prepared,
                    source=opened,
                    codec=codec,
                    cancel=normalized_cancel,
                    on_committed=on_committed,
                )
                producer_started = True
                await started.wait()
                self._messaging._require_open()
        except BaseException as error:
            if prepared is not None and prepared.is_owner and not producer_started:
                try:
                    if opened is not None:
                        await opened.aclose()
                finally:
                    try:
                        await _await_backend(
                            "finish",
                            self._messaging.backend.finish(
                                prepared.handle,
                                status="failed",
                                error=error,
                            ),
                        )
                    except BackendOwnershipLost:
                        pass
            raise
        finally:
            self._messaging._finish_preflight(preflight)

        return MessageSubscription(
            backend=self._messaging.backend,
            prepared=prepared,
            codec=cast(MessageCodec[object, ReplayT], codec),
            renderer=renderer,
        )

    async def cancel(self, *, identity: Identity) -> bool:
        """Request cancellation and wait until the durable run status is final.

        Args:
            identity: Thread and run identity whose callback should be invoked.

        Returns:
            `True` only when this call initiated cancellation and the run settled as
            cancelled. Concurrent or repeated requests return `False` after sharing
            the same final status.

        Raises:
            CancellationUnsupported: The run has no cancellation callback.
            RunNotFound: The target run does not exist.
            RunProducerFailed: The callback, encoding, append, or producer failed.
        """

        self._messaging._require_open()
        required_identity(identity)
        handle = BackendRunHandle(
            channel=self.name,
            identity=identity,
            owner_token=None,
            fence=None,
        )
        initiated = await _await_backend(
            "request_cancel",
            self._messaging.backend.request_cancel(handle),
        )
        status = await _await_backend(
            "wait_finished",
            self._messaging.backend.wait_finished(handle),
        )
        if status in {"failed", "owner_lost"}:
            cause = await _await_backend(
                "failure",
                self._messaging.backend.failure(handle),
            )
            raise RunProducerFailed(
                identity=identity,
                cause=cause
                or RuntimeError(f"Producer for run {identity.run_id!r} stopped"),
            )
        return initiated and status == "cancelled"

    async def delete_stream(self, *, identity: Identity) -> None:
        """Delete one inactive durable stream without changing the channel codec.

        Missing and previously deleted streams are successful no-ops. Deletion never
        requests producer cancellation; callers must settle an active run first.

        Args:
            identity: Identity whose thread-level durable stream is deleted.

        Raises:
            MessagingError: Messaging closes or the backend rejects deletion.
            StreamDeleteConflict: The stream still has an active producer.
            ValueError: `identity` contains a non-canonical identifier.
        """

        preflight = self._messaging._begin_preflight()
        try:
            required_identity(identity)
            await _await_backend(
                "delete_stream",
                self._messaging.backend.delete_stream(
                    channel=self.name,
                    identity=identity,
                ),
            )
            self._messaging._require_open()
        finally:
            self._messaging._finish_preflight(preflight)


class Messaging:
    """Manage producer tasks while one construction-time backend owns durable state."""

    def __init__(
        self,
        *,
        backend: MessagingBackend | None = None,
        settlement_timeout: float | None = None,
    ) -> None:
        """Configure one single-use asynchronous Messaging lifecycle.

        Args:
            backend: Durable backend owned according to its own resource contract.
            settlement_timeout: Optional per-caller close wait in seconds. ``None``
                waits until all owned producers settle. A finite timeout stops only
                the caller's wait and never cancels the shared close task.

        Raises:
            TypeError: ``settlement_timeout`` is not numeric or is a boolean.
            ValueError: ``settlement_timeout`` is negative or non-finite.
        """

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
        self._backend: MessagingBackend = backend or MemoryBackend()
        self._settlement_timeout = resolved_timeout
        self._state = "new"
        self._preflight_tasks: set[asyncio.Task[object]] = set()
        self._producer_tasks: set[asyncio.Task[None]] = set()
        self._settling_producers: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def backend(self) -> MessagingBackend:
        """Return the fixed backend that owns this facade's complete lifecycle."""

        return self._backend

    async def __aenter__(self) -> Self:
        if self._state == "closed":
            raise MessagingClosed("Messaging is closed")
        if self._state != "new":
            raise MessagingClosed("Messaging is already open")
        self._state = "open"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
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
                    f"Messaging context body also failed: {type(exc).__name__}: {exc}"
                )
                raise
            exc.add_note(
                "Messaging context cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def aclose(self) -> None:
        """Close all owned producers through one reusable settlement task.

        A finite construction-time settlement timeout limits only this caller's wait.
        The close task remains strongly owned, the facade stays unavailable to new
        operations, and a later call resumes waiting for the same task.

        Raises:
            MessagingSettlementTimeout: The configured caller wait expires.
            BaseException: Owned producer settlement fails before closure completes.
        """

        task = self._close_task
        if task is None:
            caller = cast(asyncio.Task[object] | None, asyncio.current_task())
            self._state = "closing"
            task = asyncio.create_task(
                self._close_once(caller),
                name="tinkerfin-messaging-close",
            )
            self._close_task = task
            task.add_done_callback(self._close_finished)

        timeout = self._settlement_timeout
        if timeout is None:
            await _join_owned_task(task)
            return
        if task.done():
            task.result()
            return
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                await asyncio.shield(task)
        except TimeoutError as error:
            if deadline.expired():
                raise MessagingSettlementTimeout(timeout=timeout) from error
            raise

    async def _close_once(
        self,
        initiating_caller: asyncio.Task[object] | None,
    ) -> None:
        """Wait preflight work, settle producers, and close the facade exactly once."""

        primary: BaseException | None = None
        try:
            preflights = tuple(
                task for task in self._preflight_tasks if task is not initiating_caller
            )
            if preflights:
                await asyncio.gather(*preflights, return_exceptions=True)

            producers = tuple(self._producer_tasks)
            for producer in producers:
                if not producer.done() and producer not in self._settling_producers:
                    producer.cancel()
            if producers:
                results = await asyncio.gather(
                    *producers,
                    return_exceptions=True,
                )
                for result in results:
                    if not isinstance(result, BaseException):
                        continue
                    if primary is None:
                        primary = result
                    elif result is not primary:
                        primary.add_note(
                            "Another Messaging producer also failed during close: "
                            f"{type(result).__name__}: {result}"
                        )
        finally:
            self._state = "closed"
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained close failure when no caller waits again."""

        if not task.cancelled():
            task.exception()

    def _begin_preflight(self) -> asyncio.Task[object]:
        """Register caller-owned startup work before shutdown can take its snapshot."""

        self._require_open()
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Messaging preflight requires an asyncio task")
        task = cast(asyncio.Task[object], current)
        self._preflight_tasks.add(task)
        return task

    def _finish_preflight(self, task: asyncio.Task[object]) -> None:
        self._preflight_tasks.discard(task)

    @overload
    def channel(
        self,
        *,
        name: str,
        codec: None = None,
        renderer: None = None,
    ) -> MessageChannel[Never, Never]: ...

    @overload
    def channel(
        self,
        *,
        name: str,
        codec: MessageCodec[SourceT, ReplayT],
        renderer: SseRenderer[ReplayT] | None = None,
    ) -> MessageChannel[SourceT, ReplayT]: ...

    def channel(
        self,
        *,
        name: str,
        codec: MessageCodec[SourceT, ReplayT] | None = None,
        renderer: SseRenderer[ReplayT] | None = None,
    ) -> MessageChannel[SourceT, ReplayT]:
        """Create a typed codec view over the shared backend."""

        self._require_open()
        if renderer is None and isinstance(codec, SseRenderer):
            renderer = cast(SseRenderer[ReplayT], codec)
        return MessageChannel(
            messaging=self,
            name=name,
            codec=codec,
            renderer=renderer,
        )

    def _start_producer(
        self,
        *,
        prepared: PreparedRun,
        source: MessageSource[SourceT],
        codec: MessageCodec[SourceT, ReplayT],
        cancel: _ContextCancelCallback[SourceT] | None,
        on_committed: CommittedCallback | None,
    ) -> asyncio.Event:
        def prepare_item(item: SourceT, ordinal: int) -> _ProducedMessage[SourceT]:
            return _ProducedMessage(
                data=item,
                message_id=_derived_message_id(prepared.handle.identity, ordinal),
                checkpoint=None,
            )

        return self._start_producer_task(
            prepared=prepared,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
            prepare_item=prepare_item,
        )

    async def _open_recoverable_source(
        self,
        *,
        prepared: PreparedRun,
        source: RecoverableSource[SourceT],
    ) -> MessageSource[RecoverableMessage[SourceT]]:
        """Keep distributed ownership alive while a source rebuilds its state."""

        interval = _read_backend(
            "lease_renew_interval",
            lambda: self.backend.lease_renew_interval,
        )
        if interval is None:
            return await source.open(prepared.checkpoint)

        opening = asyncio.create_task(
            source.open(prepared.checkpoint),
            name=(f"tinkerfin-messaging-source-open:{prepared.handle.identity.run_id}"),
        )

        async def renew_until_opened() -> None:
            while True:
                await asyncio.sleep(interval)
                renewed = await _await_backend(
                    "renew",
                    self.backend.renew(prepared.handle),
                )
                if not renewed:
                    raise BackendOwnershipLost(
                        "Producer for run "
                        f"{prepared.handle.identity.run_id!r} lost its lease "
                        "while reopening its source"
                    )

        renewing = asyncio.create_task(
            renew_until_opened(),
            name=(f"tinkerfin-messaging-open-lease:{prepared.handle.identity.run_id}"),
        )
        claimed = False
        try:
            done, _ = await asyncio.wait(
                {opening, renewing},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewing in done:
                renewal_error = renewing.exception()
                if renewal_error is not None:
                    if opening.done() and not opening.cancelled():
                        try:
                            opening.result()
                        except BaseException as opening_error:
                            raise renewal_error from opening_error
                    raise renewal_error
            opened = opening.result()
            claimed = True
            return opened
        finally:
            if not opening.done():
                opening.cancel()
            if not renewing.done():
                renewing.cancel()
            opening_result, _ = await asyncio.gather(
                opening,
                renewing,
                return_exceptions=True,
            )
            if not claimed and not isinstance(opening_result, BaseException):
                await opening_result.aclose()

    def _start_recoverable_producer(
        self,
        *,
        prepared: PreparedRun,
        source: MessageSource[RecoverableMessage[SourceT]],
        codec: MessageCodec[SourceT, ReplayT],
        cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None,
        on_committed: CommittedCallback | None,
    ) -> asyncio.Event:
        def prepare_item(
            item: RecoverableMessage[SourceT],
            ordinal: int,
        ) -> _ProducedMessage[SourceT]:
            del ordinal
            if item.checkpoint.last_message_id != item.message_id:
                raise ValueError(
                    "RecoverableMessage checkpoint.last_message_id must match "
                    "message_id"
                )
            return _ProducedMessage(
                data=item.data,
                message_id=item.message_id,
                checkpoint=item.checkpoint,
            )

        return self._start_producer_task(
            prepared=prepared,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
            prepare_item=prepare_item,
        )

    def _start_producer_task(
        self,
        *,
        prepared: PreparedRun,
        source: MessageSource[ProducedT],
        codec: MessageCodec[SourceT, ReplayT],
        cancel: _ContextCancelCallback[ProducedT] | None,
        on_committed: CommittedCallback | None,
        prepare_item: Callable[[ProducedT, int], _ProducedMessage[SourceT]],
    ) -> asyncio.Event:
        state = _ProducerState()
        started = asyncio.Event()
        cancel_context = CancelContext(
            channel=prepared.handle.channel,
            identity=prepared.handle.identity,
        )

        async def produce() -> None:
            # `wrap()` waits for this first turn so later task cancellation always
            # enters the producer's cleanup path and cannot strand source ownership.
            started.set()
            producer_task = asyncio.current_task()
            if producer_task is None:
                raise RuntimeError("Messaging producer requires an asyncio task")
            status: Literal["completed", "cancelled", "failed"] = "completed"
            error: BaseException | None = None
            error_stage: _ProducerFailureStage | None = None
            cancel_watcher: asyncio.Task[Iterable[ProducedT] | None] | None = None
            cancel_callback_task: asyncio.Task[Iterable[ProducedT] | None] | None = None
            settlement_task: asyncio.Task[bool] | None = None
            lease_renewer: asyncio.Task[None] | None = None
            pending_commits: asyncio.Queue[_PendingCommit[ProducedT]] = asyncio.Queue(
                maxsize=1
            )
            commit_capacity = asyncio.Semaphore(1)
            commit_error: BaseException | None = None
            next_ordinal = 1
            shutdown_requested = False

            def retain_secondary_failure(
                *,
                stage: _ProducerFailureStage,
                secondary: BaseException,
            ) -> None:
                primary = error
                primary_stage = error_stage
                if primary is None or primary_stage is None:
                    raise RuntimeError(
                        "Messaging cannot retain a secondary failure without a primary"
                    )
                primary.add_note(
                    f"Messaging {stage} also failed after {primary_stage}: "
                    f"{type(secondary).__name__}: {secondary}"
                )
                logger.error(
                    "Messaging producer retained a secondary failure",
                    extra={
                        "channel": prepared.handle.channel,
                        "thread_id": prepared.handle.identity.thread_id,
                        "run_id": prepared.handle.identity.run_id,
                        "primary_stage": primary_stage,
                        "secondary_stage": stage,
                        "ownership_lost": isinstance(
                            secondary,
                            BackendOwnershipLost,
                        ),
                    },
                    exc_info=(
                        type(secondary),
                        secondary,
                        secondary.__traceback__,
                    ),
                )

            def record_failure(
                *,
                stage: _ProducerFailureStage,
                failure: BaseException,
            ) -> None:
                nonlocal error, error_stage, status
                if error is None:
                    error = failure
                    error_stage = stage
                    status = "failed"
                    return
                if (
                    error_stage == "shutdown"
                    and isinstance(error, asyncio.CancelledError)
                    and not isinstance(failure, asyncio.CancelledError)
                ):
                    failure.add_note(
                        "Messaging shutdown also cancelled the producer before "
                        f"{stage} failed: {error}"
                    )
                    error = failure
                    error_stage = stage
                    status = "failed"
                    return
                if failure is error:
                    return
                retain_secondary_failure(stage=stage, secondary=failure)

            async def commit_messages() -> None:
                nonlocal commit_error, next_ordinal
                while True:
                    pending = await pending_commits.get()
                    try:
                        if commit_error is not None:
                            pending.error = commit_error
                            continue
                        produced = prepare_item(pending.item, next_ordinal)
                        payload = codec.encode(produced.data)
                        if not isinstance(payload, bytes):
                            raise TypeError("MessageCodec.encode() must return bytes")
                        envelope = await _await_backend(
                            "append",
                            self.backend.append(
                                prepared.handle,
                                message_id=produced.message_id,
                                codec=self._codec_id(codec),
                                payload=payload,
                                checkpoint=produced.checkpoint,
                            ),
                        )
                        next_ordinal += 1
                        if on_committed is not None:
                            try:
                                await on_committed(envelope)
                            except Exception as observer_error:  # noqa: BLE001 - observer isolation
                                logger.error(
                                    "Committed message hook failed: "
                                    "channel=%s thread_id=%s run_id=%s seq=%s "
                                    "error_type=%s",
                                    envelope.channel,
                                    envelope.identity.thread_id,
                                    envelope.identity.run_id,
                                    envelope.seq,
                                    type(observer_error).__name__,
                                )
                    except asyncio.CancelledError as append_cancellation:
                        pending.error = append_cancellation
                        raise
                    except BaseException as append_error:  # noqa: BLE001 - producer outcome
                        if commit_error is None:
                            commit_error = append_error
                        pending.error = commit_error
                    finally:
                        pending.acknowledged.set()
                        commit_capacity.release()
                        pending_commits.task_done()

            committer = asyncio.create_task(
                commit_messages(),
                name=(
                    f"tinkerfin-messaging-committer:{prepared.handle.identity.run_id}"
                ),
            )

            def enqueue_acquired(item: ProducedT) -> _PendingCommit[ProducedT]:
                pending = _PendingCommit(item=item)
                try:
                    pending_commits.put_nowait(pending)
                except BaseException:
                    commit_capacity.release()
                    raise
                return pending

            async def wait_committed(pending: _PendingCommit[ProducedT]) -> None:
                await pending.acknowledged.wait()
                if pending.error is not None:
                    raise pending.error.with_traceback(pending.error.__traceback__)

            async def submit_tail(item: ProducedT) -> None:
                await commit_capacity.acquire()
                pending = enqueue_acquired(item)
                await wait_committed(pending)

            async def consume_source() -> None:
                consumer_task = asyncio.current_task()
                if consumer_task is None:
                    raise RuntimeError("Producer source requires an asyncio task")
                iterator = aiter(source)
                while True:
                    await commit_capacity.acquire()
                    try:
                        item = await anext(iterator)
                    except StopAsyncIteration:
                        commit_capacity.release()
                        return
                    except BaseException:
                        commit_capacity.release()
                        raise
                    pending = enqueue_acquired(item)
                    await wait_committed(pending)
                    if consumer_task.cancelling():
                        raise asyncio.CancelledError

            source_consumer = asyncio.create_task(
                consume_source(),
                name=(f"tinkerfin-messaging-source:{prepared.handle.identity.run_id}"),
            )

            def claim_settlement() -> asyncio.Task[bool]:
                nonlocal settlement_task
                existing = settlement_task
                if existing is not None:
                    return existing
                settlement_task = asyncio.create_task(
                    _await_backend(
                        "begin_settlement",
                        self.backend.begin_settlement(prepared.handle),
                    ),
                    name=(
                        "tinkerfin-messaging-settlement:"
                        f"{prepared.handle.identity.run_id}"
                    ),
                )
                return settlement_task

            def claim_cancel_callback() -> tuple[
                asyncio.Task[Iterable[ProducedT] | None],
                bool,
            ]:
                nonlocal cancel_callback_task
                existing = cancel_callback_task
                if existing is not None:
                    return existing, False
                callback = cancel
                if callback is None:
                    raise RuntimeError(
                        "backend accepted cancellation without a registered callback"
                    )
                state.cancel_requested = True
                self._settling_producers.add(cast(asyncio.Task[None], producer_task))

                async def invoke() -> Iterable[ProducedT] | None:
                    try:
                        return await _invoke_cancel(callback, cancel_context)
                    except BaseException:
                        if not source_consumer.done():
                            source_consumer.cancel()
                        raise

                cancel_callback_task = asyncio.create_task(
                    invoke(),
                    name=(
                        "tinkerfin-messaging-cancel-callback:"
                        f"{prepared.handle.identity.run_id}"
                    ),
                )
                return cancel_callback_task, True

            async def watch_cancel() -> Iterable[ProducedT] | None:
                requested = await _await_backend(
                    "wait_for_cancel",
                    self.backend.wait_for_cancel(prepared.handle),
                )
                if not requested:
                    return None
                cancel_accepted = await asyncio.shield(claim_settlement())
                if not cancel_accepted:
                    return None
                callback_task, _ = claim_cancel_callback()
                return await callback_task

            if cancel is not None:
                cancel_watcher = asyncio.create_task(
                    watch_cancel(),
                    name=(
                        f"tinkerfin-messaging-cancel:{prepared.handle.identity.run_id}"
                    ),
                )

            async def renew_lease(interval: float) -> None:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        renewed = await _await_backend(
                            "renew",
                            self.backend.renew(prepared.handle),
                        )
                    except asyncio.CancelledError:
                        raise
                    except BaseException as renew_error:  # noqa: BLE001 - fence safety
                        state.ownership_lost = True
                        state.ownership_error = renew_error
                        source_consumer.cancel()
                        return
                    if not renewed:
                        state.ownership_lost = True
                        state.ownership_error = BackendOwnershipLost(
                            "Producer for run "
                            f"{prepared.handle.identity.run_id!r} lost its lease"
                        )
                        source_consumer.cancel()
                        return

            renew_interval = _read_backend(
                "lease_renew_interval",
                lambda: self.backend.lease_renew_interval,
            )
            if renew_interval is not None:
                lease_renewer = asyncio.create_task(
                    renew_lease(renew_interval),
                    name=(
                        f"tinkerfin-messaging-lease:{prepared.handle.identity.run_id}"
                    ),
                )
            try:
                await asyncio.shield(source_consumer)
                if state.cancel_requested:
                    status = "cancelled"
            except asyncio.CancelledError as cancellation:
                producer_task = asyncio.current_task()
                shutdown_requested = (
                    producer_task is not None and producer_task.cancelling() > 0
                )
                if state.ownership_lost:
                    record_failure(
                        stage="lease",
                        failure=state.ownership_error or cancellation,
                    )
                else:
                    status = "cancelled" if state.cancel_requested else "failed"
                    if state.cancel_requested:
                        error = None
                        error_stage = None
                    else:
                        error = cancellation
                        error_stage = "shutdown" if shutdown_requested else "source"
            except BackendOwnershipLost as ownership_error:
                state.ownership_lost = True
                record_failure(stage="lease", failure=ownership_error)
            except BaseException as producer_error:  # noqa: BLE001 - record producer outcome
                record_failure(
                    stage=("commit" if producer_error is commit_error else "source"),
                    failure=producer_error,
                )
            finally:
                producer_task = asyncio.current_task()
                if producer_task is not None:
                    self._settling_producers.add(
                        cast(asyncio.Task[None], producer_task)
                    )
                cancel_won = False
                try:
                    cancel_won = await asyncio.shield(claim_settlement())
                except BackendOwnershipLost as ownership_error:
                    state.ownership_lost = True
                    record_failure(stage="settlement", failure=ownership_error)
                except BaseException as settlement_error:  # noqa: BLE001
                    record_failure(stage="settlement", failure=settlement_error)
                state.settling = True
                cancel_watcher_settled = False
                cancel_error: BaseException | None = None
                cancel_tail: Iterable[ProducedT] | None = None

                if cancel_won:
                    if not state.ownership_lost and (
                        status == "completed"
                        or shutdown_requested
                        or isinstance(error, asyncio.CancelledError)
                    ):
                        status = "cancelled"
                        error = None
                        error_stage = None
                    try:
                        callback_task, claimed_here = claim_cancel_callback()
                    except BaseException as callback_error:  # noqa: BLE001
                        cancel_error = callback_error
                    else:
                        if (
                            claimed_here
                            and cancel_watcher is not None
                            and not cancel_watcher.done()
                        ):
                            cancel_watcher.cancel()
                            await asyncio.gather(
                                cancel_watcher,
                                return_exceptions=True,
                            )
                            cancel_watcher_settled = True
                        try:
                            cancel_tail = await callback_task
                        except BaseException as callback_error:  # noqa: BLE001
                            cancel_error = callback_error
                        if cancel_watcher is not None and not cancel_watcher_settled:
                            await asyncio.gather(
                                cancel_watcher,
                                return_exceptions=True,
                            )
                            cancel_watcher_settled = True
                elif cancel_watcher is not None:
                    if not cancel_watcher.done():
                        cancel_watcher.cancel()
                    await asyncio.gather(
                        cancel_watcher,
                        return_exceptions=True,
                    )
                    cancel_watcher_settled = True

                if not source_consumer.done():
                    source_consumer.cancel()
                await asyncio.gather(source_consumer, return_exceptions=True)

                if shutdown_requested and not cancel_won:
                    if cancel_watcher is not None:
                        cancel_watcher_settled = True
                    if not committer.done():
                        committer.cancel()
                    await asyncio.gather(committer, return_exceptions=True)
                    while True:
                        try:
                            pending = pending_commits.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        pending.error = asyncio.CancelledError()
                        pending.acknowledged.set()
                        commit_capacity.release()
                        pending_commits.task_done()
                if cancel_error is not None:
                    record_failure(stage="callback", failure=cancel_error)
                elif cancel_tail is not None:
                    try:
                        for item in cancel_tail:
                            await submit_tail(item)
                    except BaseException as tail_error:  # noqa: BLE001
                        record_failure(
                            stage=("commit" if tail_error is commit_error else "tail"),
                            failure=tail_error,
                        )
                await pending_commits.join()
                if commit_error is not None:
                    record_failure(stage="commit", failure=commit_error)
                if not committer.done():
                    committer.cancel()
                committer_result = await asyncio.gather(
                    committer,
                    return_exceptions=True,
                )
                unexpected_committer_error = next(
                    (
                        result
                        for result in committer_result
                        if isinstance(result, BaseException)
                        and not isinstance(result, asyncio.CancelledError)
                    ),
                    None,
                )
                if unexpected_committer_error is not None:
                    record_failure(
                        stage="commit",
                        failure=unexpected_committer_error,
                    )
                try:
                    await source.aclose()
                except BaseException as close_error:  # noqa: BLE001 - cleanup is part of outcome
                    record_failure(stage="source_close", failure=close_error)
                try:
                    await _await_backend(
                        "finish",
                        self.backend.finish(
                            prepared.handle,
                            status=status,
                            error=error,
                        ),
                    )
                except BaseException as finish_error:
                    if error is None:
                        raise
                    record_failure(stage="finish", failure=finish_error)
                    assert error is not None
                    raise error.with_traceback(error.__traceback__)
                finally:
                    if cancel_watcher is not None and not cancel_watcher_settled:
                        if not cancel_watcher.done():
                            cancel_watcher.cancel()
                        await asyncio.gather(
                            cancel_watcher,
                            return_exceptions=True,
                        )
                    if lease_renewer is not None:
                        if not lease_renewer.done():
                            lease_renewer.cancel()
                        await asyncio.gather(lease_renewer, return_exceptions=True)

        task = asyncio.create_task(
            produce(),
            name=(f"tinkerfin-messaging-producer:{prepared.handle.identity.run_id}"),
        )
        self._producer_tasks.add(task)
        task.add_done_callback(self._producer_finished)
        return started

    def _producer_finished(self, task: asyncio.Task[None]) -> None:
        """Remove and consume a settled owned task to prevent orphan warnings."""

        self._producer_tasks.discard(task)
        self._settling_producers.discard(task)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _codec_id(codec: MessageCodec[SourceT, ReplayT]) -> str:
        return codec.codec_id

    def _require_open(self) -> None:
        if self._state == "new":
            raise MessagingNotStarted("Messaging is not started")
        if self._state != "open":
            raise MessagingClosed("Messaging is closed")
