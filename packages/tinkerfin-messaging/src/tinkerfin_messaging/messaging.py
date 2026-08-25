"""Protocol-neutral producer, replay subscription, and SSE facade."""

from __future__ import annotations

import asyncio
import math
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
)
from dataclasses import dataclass
from types import TracebackType
from typing import Generic, Never, Self, TypeAlias, TypeVar, cast, overload

from tinkerfin_agui_adapter import Identity

from . import _message_channel, _messaging_boundary, _producer_runtime
from ._identity import required_identifier, required_identity
from ._messaging_boundary import (
    _invoke_cancel as _invoke_cancel,
)
from ._messaging_boundary import (
    _iterate_backend,
    _join_owned_task,
    _read_backend,
)
from ._messaging_boundary import (
    _normalize_cancel_callback as _normalize_cancel_callback,
)
from ._producer_runtime import _ProducedMessage
from .backend import MemoryBackend, MessagingBackend, PreparedRun
from .errors import (
    CodecMismatch,
    MessagingClosed,
    MessagingNotStarted,
    MessagingSettlementTimeout,
)
from .models import (
    DecodedMessage,
    MessageEnvelope,
    RecoverableMessage,
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


@dataclass(frozen=True, slots=True)
class CancelContext:
    """Identify the run whose accepted cancellation invokes a callback."""

    channel: str
    identity: Identity

    def __post_init__(self) -> None:
        """Validate the durable channel and run identity before callback use."""

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
        """Initialize a lazy single-use decoder over one prepared run."""

        self._backend = backend
        self._prepared = prepared
        self._codec = codec
        self._renderer = renderer
        self._claimed = False
        self._delivery: AsyncIterator[DecodedMessage[ReplayT]] | None = None
        self._backend_iterator: AsyncIterator[object] | None = None
        self._backend_close_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    def __aiter__(self) -> AsyncIterator[DecodedMessage[ReplayT]]:
        """Claim and return the subscription's one decoded iterator."""

        return _messaging_boundary.__aiter__(
            self,
        )

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
                await _messaging_boundary._close_backend_iterator(
                    self,
                    cast(AsyncIterator[object], backend_iterator),
                )
            except BaseException as close_error:
                if primary is None:
                    raise
                primary.add_note(f"Messaging follow cleanup also failed: {close_error}")

    def sse(self) -> AsyncIterator[bytes]:
        """Render committed messages while preserving their durable sequences."""

        return _messaging_boundary.sse(
            self,
        )

    async def aclose(self) -> None:
        """Detach this subscriber without affecting the producer."""

        return await _messaging_boundary.aclose(
            self,
        )


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
        """Initialize a named facade that borrows its parent Messaging lifecycle."""

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

        return _message_channel._resolve_identity(
            source,
            identity,
        )

    def _resolve_binding(
        self,
        source: object,
    ) -> tuple[
        MessageCodec[SourceT, ReplayT],
        SseRenderer[ReplayT] | None,
        str | None,
    ]:
        """Resolve an explicit codec or one supported structural source profile."""

        return _message_channel._resolve_binding(
            self,
            source,
        )

    @staticmethod
    def _validate_profile_types(
        *,
        source: object,
        profile: str,
        codec: object,
    ) -> None:
        """Prove declared live and replay types before durable preparation."""

        return _message_channel._validate_profile_types(
            source=source,
            profile=profile,
            codec=codec,
        )

    def _commit_inferred_binding(
        self,
        *,
        codec: MessageCodec[SourceT, ReplayT],
        renderer: SseRenderer[ReplayT] | None,
        profile: str | None,
    ) -> None:
        return _message_channel._commit_inferred_binding(
            self,
            codec=codec,
            renderer=renderer,
            profile=profile,
        )

    def _require_read_codec(self) -> MessageCodec[SourceT, ReplayT]:
        return _message_channel._require_read_codec(
            self,
        )

    @staticmethod
    def _validate_page(*, after: int, limit: int | None = None) -> None:
        return _message_channel._validate_page(
            after=after,
            limit=limit,
        )

    async def latest_seq(self, *, identity: Identity) -> int:
        """Return the greatest committed sequence, or zero for an empty stream."""

        return await _message_channel.latest_seq(
            self,
            identity=identity,
        )

    async def read(
        self,
        *,
        identity: Identity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[DecodedMessage[ReplayT], ...]:
        """Decode one ascending committed page after an exclusive cursor."""

        return await _message_channel.read(
            self,
            identity=identity,
            after=after,
            limit=limit,
        )

    async def follow(
        self,
        *,
        identity: Identity,
        after: int = 0,
    ) -> MessageSubscription[ReplayT]:
        """Follow one run's committed events through its authoritative terminal."""

        return await _message_channel.follow(
            self,
            identity=identity,
            after=after,
        )

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

        return await _message_channel.validate_cursor(
            self,
            identity=identity,
            after=after,
        )

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
        """Start or attach one source and return its run-bounded subscription."""

        return await _message_channel.wrap(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
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

        return await _message_channel._wrap(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
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

        return await _message_channel.sse(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
        )

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

        return await _message_channel.wrap_recoverable(
            self,
            source,
            identity=identity,
            after=after,
            cancel=cancel,
            on_committed=on_committed,
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

        return await _message_channel.cancel(
            self,
            identity=identity,
        )

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

        return await _message_channel.delete_stream(
            self,
            identity=identity,
        )


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
        """Open this single-use Messaging lifecycle."""

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
        """Close Messaging while preserving the active scope's primary failure."""

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
            # A cancellation preflight can be waiting for the producer's durable
            # terminal. Signal existing producers before joining preflights so close,
            # cancel, and settlement cannot form a wait cycle.
            for producer in tuple(self._producer_tasks):
                if not producer.done() and producer not in self._settling_producers:
                    producer.cancel()
            preflights = tuple(
                task for task in self._preflight_tasks if task is not initiating_caller
            )
            if preflights:
                await asyncio.gather(*preflights, return_exceptions=True)

            # A startup preflight may have published a producer before observing the
            # closing state. Re-snapshot after it settles and cancel any such owner.
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
        return _producer_runtime._start_producer(
            self,
            prepared=prepared,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
        )

    async def _open_recoverable_source(
        self,
        *,
        prepared: PreparedRun,
        source: RecoverableSource[SourceT],
    ) -> MessageSource[RecoverableMessage[SourceT]]:
        """Keep distributed ownership alive while a source rebuilds its state."""

        return await _producer_runtime._open_recoverable_source(
            self,
            prepared=prepared,
            source=source,
        )

    def _start_recoverable_producer(
        self,
        *,
        prepared: PreparedRun,
        source: MessageSource[RecoverableMessage[SourceT]],
        codec: MessageCodec[SourceT, ReplayT],
        cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None,
        on_committed: CommittedCallback | None,
    ) -> asyncio.Event:
        return _producer_runtime._start_recoverable_producer(
            self,
            prepared=prepared,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
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
        return _producer_runtime._start_producer_task(
            self,
            prepared=prepared,
            source=source,
            codec=codec,
            cancel=cancel,
            on_committed=on_committed,
            prepare_item=prepare_item,
        )

    def _producer_finished(self, task: asyncio.Task[None]) -> None:
        """Remove and consume a settled owned task to prevent orphan warnings."""

        return _producer_runtime._producer_finished(
            self,
            task,
        )

    @staticmethod
    def _codec_id(codec: MessageCodec[SourceT, ReplayT]) -> str:
        return _producer_runtime._codec_id(
            codec,
        )

    def _require_open(self) -> None:
        if self._state == "new":
            raise MessagingNotStarted("Messaging is not started")
        if self._state != "open":
            raise MessagingClosed("Messaging is closed")
