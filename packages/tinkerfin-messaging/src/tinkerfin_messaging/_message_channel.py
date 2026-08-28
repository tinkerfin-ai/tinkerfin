"""Message channel binding, replay, publication, and control operations."""

from __future__ import annotations

__all__ = [
    "_commit_inferred_binding",
    "_require_read_codec",
    "_resolve_binding",
    "_resolve_identity",
    "_validate_page",
    "_validate_profile_types",
    "_wrap",
    "get_run_status",
]

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, TypeAlias, TypeVar, cast

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity
from ._messaging_boundary import (
    _await_backend,
    _ContextCancelCallback,
    _normalize_cancel_callback,
    _validate_optional_cursor,
)
from .backend import BackendRunHandle, PreparedRun, RunStatus
from .errors import (
    BackendOwnershipLost,
    CodecMismatch,
    InvalidCursor,
    MessagingBackendProtocolError,
    RunProducerFailed,
    SourceProfileMismatch,
)
from .models import DecodedMessage, RecoverableMessage
from .protocols import (
    MessageCodec,
    MessageSource,
    ProfiledMessageSource,
    RecoverableSource,
    SseRenderer,
)

if TYPE_CHECKING:
    from .messaging import (
        CancelCallback,
        CommittedCallback,
        MessageChannel,
        MessageSubscription,
    )

SourceT = TypeVar("SourceT")
ReplayT = TypeVar("ReplayT")
ProfileSourceT = TypeVar("ProfileSourceT")
ProfileReplayT = TypeVar("ProfileReplayT")
_RUN_STATUSES = frozenset(
    {"running", "cancel_requested", "completed", "cancelled", "failed", "owner_lost"}
)
_CodecInputTransform: TypeAlias = Callable[[object], object]


def _resolve_identity(source: object, identity: RunIdentity | None) -> RunIdentity:
    """Resolve one explicit or immutable source identity before side effects."""

    explicit = None if identity is None else required_identity(identity)
    source_identity = getattr(source, "messaging_identity", None)
    profiled = None if source_identity is None else required_identity(source_identity)
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
    self: MessageChannel[SourceT, ReplayT],
    source: object,
) -> tuple[
    MessageCodec[SourceT, ReplayT],
    SseRenderer[ReplayT] | None,
    str | None,
    _CodecInputTransform | None,
]:
    """Resolve an explicit codec or one supported structural source profile."""

    if bool(getattr(source, "is_tinkerfin_sse_body", False)):
        raise TypeError("Messaging requires object events, not pre-encoded SSE frames")
    codec = self._codec
    inferred_profile = self._inferred_profile
    if codec is not None and inferred_profile is None:
        return codec, self._renderer, None, None
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
        codec_input = self._validate_profile_types(
            source=source,
            profile=profile,
            codec=codec,
        )
        return codec, self._renderer, profile, codec_input
    if profile == "agui.event":
        from .agui import AgUiCodec

        built_in = AgUiCodec()
    elif profile == "tinkerfin.native-stream":
        from .native import NativeStreamPartCodec

        built_in = NativeStreamPartCodec()
    else:
        raise TypeError(f"unsupported built-in codec profile: {profile!r}")
    codec_input = self._validate_profile_types(
        source=source,
        profile=profile,
        codec=built_in,
    )
    current = self._inferred_profile
    if current is not None and current != profile:
        raise CodecMismatch(expected=current, actual=profile)
    return (
        cast(MessageCodec[SourceT, ReplayT], built_in),
        cast(SseRenderer[ReplayT], built_in),
        profile,
        codec_input,
    )


def _validate_profile_types(
    *,
    source: object,
    profile: str,
    codec: object,
) -> _CodecInputTransform | None:
    """Prove live, optional codec-input, and replay types before preparation."""

    missing = object()
    declared_source = getattr(source, "messaging_source_type", missing)
    if declared_source is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="missing live source type metadata",
        )
    if not isinstance(declared_source, type):
        raise SourceProfileMismatch(
            profile=profile,
            reason="live source type metadata must be a type",
        )
    expected_source = getattr(codec, "messaging_source_type", missing)
    if expected_source is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="built-in codec is missing codec input type metadata",
        )

    raw_transform = getattr(source, "messaging_codec_input", missing)
    if raw_transform is missing:
        if declared_source is not expected_source:
            expected_name = getattr(
                expected_source,
                "__qualname__",
                type(expected_source).__name__,
            )
            raise SourceProfileMismatch(
                profile=profile,
                reason=f"live source type metadata must be {expected_name}",
            )
        codec_input: _CodecInputTransform | None = None
    else:
        if not callable(raw_transform):
            raise SourceProfileMismatch(
                profile=profile,
                reason="codec input transform must be callable",
            )
        declared_codec_input = getattr(
            source,
            "messaging_codec_input_type",
            missing,
        )
        if declared_codec_input is missing:
            raise SourceProfileMismatch(
                profile=profile,
                reason="missing codec input type metadata",
            )
        if declared_codec_input is not expected_source:
            expected_name = getattr(
                expected_source,
                "__qualname__",
                type(expected_source).__name__,
            )
            raise SourceProfileMismatch(
                profile=profile,
                reason=f"codec input type metadata must be {expected_name}",
            )
        codec_input = cast(_CodecInputTransform, raw_transform)

    declared_replay = getattr(source, "messaging_replay_type", missing)
    if declared_replay is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="missing replay type metadata",
        )
    expected_replay = getattr(codec, "messaging_replay_type", missing)
    if expected_replay is missing:
        raise SourceProfileMismatch(
            profile=profile,
            reason="built-in codec is missing replay type metadata",
        )
    if declared_replay is not expected_replay:
        expected_name = getattr(
            expected_replay,
            "__qualname__",
            type(expected_replay).__name__,
        )
        raise SourceProfileMismatch(
            profile=profile,
            reason=f"replay type metadata must be {expected_name}",
        )
    return codec_input


def _commit_inferred_binding(
    self: MessageChannel[SourceT, ReplayT],
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


def _require_read_codec(
    self: MessageChannel[SourceT, ReplayT],
) -> MessageCodec[SourceT, ReplayT]:
    codec = self._codec
    if codec is None:
        raise TypeError(
            "channel codec is unknown; provide one explicitly or infer it from "
            "a profiled source first"
        )
    return codec


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


async def latest_seq(
    self: MessageChannel[SourceT, ReplayT], *, identity: RunIdentity
) -> int:
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


async def get_run_status(
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
) -> RunStatus:
    """Return one durable run's current authoritative status."""

    preflight = self._messaging._begin_preflight()
    try:
        required_identity(identity)
        self._messaging._require_open()
        status: object = await _await_backend(
            "get_run_status",
            self._messaging.backend.get_run_status(
                channel=self.name,
                identity=identity,
            ),
        )
        self._messaging._require_open()
        if not isinstance(status, str) or status not in _RUN_STATUSES:
            raise MessagingBackendProtocolError(
                "Messaging backend returned an invalid run status",
                diagnostic_context={
                    "operation": "get_run_status",
                    "actual_type": type(status).__name__,
                },
            )
        return status
    finally:
        self._messaging._finish_preflight(preflight)


async def read(
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
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
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
    after: int = 0,
) -> MessageSubscription[ReplayT]:
    """Follow one run's committed events through its authoritative terminal."""

    from .messaging import MessageSubscription

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
                after=after,
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
    self: MessageChannel[SourceT, ReplayT],
    *,
    identity: RunIdentity,
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


async def wrap(
    self: MessageChannel[SourceT, ReplayT],
    source: (
        MessageSource[SourceT] | ProfiledMessageSource[ProfileSourceT, ProfileReplayT]
    ),
    *,
    identity: RunIdentity | None = None,
    after: int | None = None,
    cancel: (CancelCallback[SourceT] | CancelCallback[ProfileSourceT] | None) = None,
    on_committed: CommittedCallback | None = None,
) -> MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT]:
    """Open a replayable subscription while preserving the source profile type."""

    return cast(
        "MessageSubscription[ReplayT] | MessageSubscription[ProfileReplayT]",
        await self._wrap(
            cast(MessageSource[object], source),
            identity=identity,
            after=after,
            cancel=cast("CancelCallback[object] | None", cancel),
            on_committed=on_committed,
        ),
    )


async def _wrap(
    self: MessageChannel[SourceT, ReplayT],
    source: MessageSource[object],
    *,
    identity: RunIdentity | None = None,
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

    from .messaging import MessageSubscription

    preflight = self._messaging._begin_preflight()
    prepared: PreparedRun | None = None
    normalized_cancel: _ContextCancelCallback[object] | None = None
    producer_started = False
    source_released = False
    try:
        _validate_optional_cursor(after)
        codec, renderer, profile, codec_input = self._resolve_binding(source)
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
            cancel = cast("CancelCallback[object]", source_cancel)
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
        if prepared.is_owner:
            owner_preflight = getattr(source, "messaging_owner_preflight", None)
            if owner_preflight is not None:
                if not callable(owner_preflight):
                    raise TypeError("messaging_owner_preflight must be async callable")
                callback = cast(Callable[[], Awaitable[None]], owner_preflight)
                await callback()
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
                codec_input=codec_input,
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


async def sse(
    self: MessageChannel[SourceT, ReplayT],
    source: MessageSource[object],
    *,
    identity: RunIdentity | None = None,
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
    self: MessageChannel[SourceT, ReplayT],
    source: RecoverableSource[SourceT],
    *,
    identity: RunIdentity | None = None,
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

    from .messaging import MessageSubscription

    preflight = self._messaging._begin_preflight()
    prepared: PreparedRun | None = None
    opened: MessageSource[RecoverableMessage[SourceT]] | None = None
    normalized_cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None = None
    producer_started = False
    try:
        _validate_optional_cursor(after)
        codec, renderer, profile, codec_input = self._resolve_binding(source)
        if codec_input is not None:
            raise SourceProfileMismatch(
                profile=profile or codec.codec_id,
                reason="recoverable source profiles must yield codec input directly",
            )
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


async def cancel(
    self: MessageChannel[SourceT, ReplayT], *, identity: RunIdentity
) -> bool:
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

    preflight = self._messaging._begin_preflight()
    try:
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
    finally:
        self._messaging._finish_preflight(preflight)


async def delete_stream(
    self: MessageChannel[SourceT, ReplayT], *, identity: RunIdentity
) -> None:
    """Delete one inactive durable stream without changing the channel codec.

    Missing and previously deleted streams are successful no-ops. Deletion never
    requests producer cancellation; callers must settle an active run first.

    Args:
        identity: RunIdentity whose thread-level durable stream is deleted.

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
