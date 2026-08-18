"""Stable public failures for protocol-neutral messaging operations."""

from __future__ import annotations


class MessagingError(Exception):
    """Base failure whose message is safe to expose at a host boundary."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        self.message = message
        self.cause = cause
        super().__init__(message)


class MessagingNotStarted(MessagingError):
    """The Messaging facade has not entered its asynchronous lifecycle."""


class MessagingSettlementTimeout(MessagingError):
    """A caller stopped waiting before Messaging settlement completed."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"Messaging settlement timed out after {timeout:g} seconds")


class MessagingClosed(MessagingError):
    """The single-use Messaging facade has already closed."""


class InvalidCursor(MessagingError):
    """A replay cursor falls outside the retained stream range."""

    def __init__(self, *, after: int, latest: int) -> None:
        self.after = after
        self.latest = latest
        super().__init__(
            f"Replay cursor {after} is outside the retained range 0..{latest}"
        )


class CodecMismatch(MessagingError):
    """One logical channel stream was opened with a different durable codec."""

    def __init__(self, *, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Stream codec is {expected!r}; it cannot be read as {actual!r}"
        )


class SourceProfileMismatch(MessagingError):
    """A built-in source profile is missing or contradicts its codec types."""

    def __init__(self, *, profile: str, reason: str) -> None:
        self.profile = profile
        self.reason = reason
        super().__init__(f"Source profile {profile!r} is incompatible: {reason}")


class MessageIdConflict(MessagingError):
    """A message ID was retried with different committed content."""

    def __init__(self, *, stream: str, message_id: str) -> None:
        self.stream = stream
        self.message_id = message_id
        super().__init__(
            f"Message {message_id!r} already exists in stream {stream!r} "
            "with different content"
        )


class RunAlreadyActive(MessagingError):
    """A different run already owns one channel and stream slot."""

    def __init__(self, *, active_run: str, requested_run: str) -> None:
        self.active_run = active_run
        self.requested_run = requested_run
        super().__init__(
            f"Run {active_run!r} is already active; cannot start {requested_run!r}"
        )


class RunIdentityConflict(MessagingError):
    """A run was attached with a different caller-defined identity."""

    def __init__(self, *, run: str) -> None:
        self.run = run
        super().__init__(f"Run {run!r} already exists with a different identity")


class RunNotFound(MessagingError):
    """No run record exists for the requested channel and stream."""

    def __init__(self, *, run: str) -> None:
        self.run = run
        super().__init__(f"Run {run!r} was not found")


class RunProducerFailed(MessagingError):
    """A producer failed after zero or more messages were committed."""

    def __init__(self, *, run: str, cause: BaseException) -> None:
        self.run = run
        super().__init__(f"Producer for run {run!r} failed", cause=cause)


class CancellationUnsupported(MessagingError):
    """The active producer has no application cancellation callback."""

    def __init__(self, *, run: str) -> None:
        self.run = run
        super().__init__(f"Run {run!r} does not support cancellation")


class RecoveryUnsupported(MessagingError):
    """A non-recoverable source cannot be rebuilt after owner loss."""


class SseRenderingUnsupported(MessagingError):
    """A channel has no SSE renderer for its decoded payload type."""


class BackendOwnershipLost(MessagingError):
    """A stale producer attempted to mutate a run after losing ownership."""


class StreamDeleted(MessagingError):
    """A handle refers to a stream generation that has been deleted."""

    def __init__(
        self,
        *,
        channel: str,
        stream: str,
        generation: int | None,
    ) -> None:
        self.channel = channel
        self.stream = stream
        self.generation = generation
        generation_text = (
            "the current generation"
            if generation is None
            else f"generation {generation}"
        )
        super().__init__(
            f"Stream {stream!r} in channel {channel!r} {generation_text} "
            "has been deleted"
        )


class StreamDeleteConflict(MessagingError):
    """A stream cannot be deleted while one producer lease is active."""

    def __init__(self, *, channel: str, stream: str, active_run: str) -> None:
        self.channel = channel
        self.stream = stream
        self.active_run = active_run
        super().__init__(
            f"Stream {stream!r} in channel {channel!r} has active run {active_run!r}"
        )
