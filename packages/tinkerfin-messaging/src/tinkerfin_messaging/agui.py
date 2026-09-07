"""Optional AG-UI event codec and native SSE renderer."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import ClassVar, Protocol, cast

from ag_ui.core import (
    ActivityMessage,
    AssistantMessage,
    BaseEvent,
    DeveloperMessage,
    Event,
    Message,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedOutcome,
    RunStartedEvent,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from pydantic import BaseModel, JsonValue, TypeAdapter

from tinkerfin_contracts import RunIdentity

from ._identity import required_identity
from .protocols import MessageCodec, MessageSource, ProfiledMessageSource, SseRenderer
from .sources import (
    MessageSourceBinding,
    ProfiledDeferredMessageSource,
    map_source,
)

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_STABLE_EVENT_FIELDS = (
    "thread_id",
    "run_id",
    "parent_run_id",
    "message_id",
    "tool_call_id",
    "parent_message_id",
    "entity_id",
    "role",
    "name",
    "tool_call_name",
    "activity_type",
    "step_name",
    "subtype",
    "code",
    "source",
    "replace",
)


class _RawEventCarrier(Protocol):
    raw_event: object


def _protocol_model_type(value: BaseModel) -> type[BaseModel]:
    """Compare standard AG-UI identity while permitting typed extension fields.

    The durable AG-UI union reconstructs standard models, keeping extension data
    as extras. A declared subclass therefore has the identity of its nearest
    AG-UI model, without weakening discriminator or correlation field checks.
    """
    for model_type in type(value).__mro__:
        if model_type.__module__.startswith("ag_ui.core."):
            return model_type
    return type(value)


def _tool_call_identity(value: ToolCall) -> tuple[object, ...]:
    function = value.function
    return (
        _protocol_model_type(value),
        value.id,
        value.type,
        function.name,
        function.arguments,
    )


def _snapshot_message_identity(value: Message) -> tuple[object, ...]:
    raw_tool_calls = value.tool_calls if isinstance(value, AssistantMessage) else None
    tool_calls = (
        tuple(_tool_call_identity(item) for item in raw_tool_calls)
        if raw_tool_calls is not None
        else None
    )
    named_message = isinstance(
        value,
        DeveloperMessage | SystemMessage | AssistantMessage | UserMessage,
    )
    return (
        _protocol_model_type(value),
        value.id,
        value.role,
        value.name if named_message else None,
        value.tool_call_id if isinstance(value, ToolMessage) else None,
        value.activity_type if isinstance(value, ActivityMessage) else None,
        tool_calls,
    )


def _freeze_protocol_value(value: JsonValue) -> object:
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _freeze_protocol_value(item)) for key, item in value.items()
            )
        )
    if isinstance(value, list):
        return tuple(_freeze_protocol_value(item) for item in value)
    return value


def _run_outcome_identity(value: RunFinishedOutcome) -> tuple[object, ...]:
    raw_interrupts = (
        value.interrupts if isinstance(value, RunFinishedInterruptOutcome) else None
    )
    interrupts = (
        tuple(
            (
                type(item),
                item.id,
                item.reason,
                item.tool_call_id,
                _freeze_protocol_value(
                    None
                    if item.response_schema is None
                    else _JSON_VALUE_ADAPTER.validate_python(item.response_schema)
                ),
                item.expires_at,
            )
            for item in raw_interrupts
        )
        if raw_interrupts is not None
        else None
    )
    return _protocol_model_type(value), value.type, interrupts


def _run_input_identity(event: BaseEvent) -> object:
    """Freeze the caller's complete input without collapsing alias extras."""

    if not isinstance(event, RunStartedEvent) or event.input is None:
        return None
    value = event.input.model_dump(
        mode="json",
        by_alias=False,
        exclude_none=False,
        serialize_as_any=True,
    )
    return _freeze_protocol_value(_JSON_VALUE_ADAPTER.validate_python(value))


def _interrupt_metadata(event: BaseEvent) -> tuple[JsonValue | None, ...]:
    if not isinstance(event, RunFinishedEvent) or not isinstance(
        event.outcome,
        RunFinishedInterruptOutcome,
    ):
        return ()
    return tuple(
        None
        if interrupt.metadata is None
        else _JSON_VALUE_ADAPTER.validate_python(deepcopy(interrupt.metadata))
        for interrupt in event.outcome.interrupts
    )


def _metadata_extends(expected: JsonValue, actual: JsonValue) -> bool:
    """Allow new product metadata without changing any existing metadata value."""

    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _metadata_extends(item, actual[key])
            for key, item in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _metadata_extends(left, right)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    return expected == actual


def _event_protocol_identity(event: BaseEvent) -> tuple[object, ...]:
    """Freeze correlation and lifecycle fields while leaving product content mutable."""

    fields = type(event).model_fields
    stable = tuple(
        (name, getattr(event, name)) for name in _STABLE_EVENT_FIELDS if name in fields
    )
    messages = (
        tuple(_snapshot_message_identity(message) for message in event.messages)
        if isinstance(event, MessagesSnapshotEvent)
        else None
    )
    outcome = (
        _run_outcome_identity(event.outcome)
        if isinstance(event, RunFinishedEvent) and event.outcome is not None
        else None
    )
    return (
        _protocol_model_type(event),
        event.type,
        stable,
        messages,
        outcome,
        _run_input_identity(event),
    )


def _validate_run_started_input(event: BaseEvent) -> None:
    """Keep an optional Run input on the same lineage as its start event."""

    if not isinstance(event, RunStartedEvent) or event.input is None:
        return
    run_input = event.input
    if (
        run_input.thread_id != event.thread_id
        or run_input.run_id != event.run_id
        or run_input.parent_run_id != event.parent_run_id
    ):
        raise ValueError("AG-UI event RunIdentity input does not match its start event")


def _canonical_event(
    event: BaseEvent,
    *,
    expected_type: type[BaseModel],
    expected_identity: tuple[object, ...],
    expected_interrupt_metadata: tuple[JsonValue | None, ...],
) -> BaseEvent:
    """Normalize through durable aliases and prove protocol identity is unchanged."""

    canonical = _EVENT_ADAPTER.validate_json(
        event.model_dump_json(
            by_alias=True,
            exclude_none=False,
            serialize_as_any=True,
        )
    )
    if _protocol_model_type(canonical) is not expected_type:
        raise TypeError("AG-UI event type changed during canonical encoding")
    if _event_protocol_identity(canonical) != expected_identity:
        raise ValueError("AG-UI event changed protocol identity")
    actual_interrupt_metadata = _interrupt_metadata(canonical)
    if len(actual_interrupt_metadata) != len(expected_interrupt_metadata) or any(
        expected is not None and not _metadata_extends(expected, actual)
        for expected, actual in zip(
            expected_interrupt_metadata,
            actual_interrupt_metadata,
            strict=True,
        )
    ):
        raise ValueError("AG-UI event changed existing interrupt metadata")
    _validate_run_started_input(canonical)
    return canonical


def _validate_event_run_identity(event: BaseEvent, identity: RunIdentity) -> None:
    if isinstance(event, RunStartedEvent | RunFinishedEvent) and (
        event.thread_id != identity.thread_id or event.run_id != identity.run_id
    ):
        raise ValueError("AG-UI event RunIdentity does not match its source")
    if not isinstance(event, RunErrorEvent):
        return
    raw_event_value = cast(_RawEventCarrier, event).raw_event
    if not isinstance(raw_event_value, dict):
        return
    raw_event = _JSON_VALUE_ADAPTER.validate_python(raw_event_value)
    if not isinstance(raw_event, dict):
        return
    for field, expected in (
        ("threadId", identity.thread_id),
        ("runId", identity.run_id),
    ):
        value = raw_event.get(field)
        if value is not None and value != expected:
            raise ValueError("AG-UI error RunIdentity does not match its source")


def create_agui_run_source(
    identity: RunIdentity,
    *,
    open_events: Callable[
        [RunIdentity],
        Awaitable[MessageSource[BaseEvent]],
    ],
    transform_event: (
        Callable[[BaseEvent], BaseEvent | Awaitable[BaseEvent]] | None
    ) = None,
) -> ProfiledMessageSource[BaseEvent, BaseEvent]:
    """Create a durable-ready AG-UI source from one lazy managed run opener.

    The helper keeps model, Sandbox, and Graph setup behind Messaging's owner decision.
    Attachments therefore never call ``open_events``. A newly selected owner receives
    the exact ``RunIdentity`` object supplied here; the returned stream must publish the
    same identity and declare that cancellation waits for its first lifecycle event.

    Args:
        identity: Single authoritative thread and run identity used for preparation and
            managed Runtime opening.
        open_events: Async owner-only function receiving that exact identity and
            returning one single-use AG-UI event source.
        transform_event: Optional synchronous or asynchronous product metadata
            or content transform, applied with source backpressure before durable
            encoding. It must preserve the event type, lifecycle role, and every
            protocol correlation identity. A Run start's complete caller input is
            authoritative and cannot be transformed.

    Returns:
        A profiled lazy source ready for an AG-UI-inferred Messaging channel.

    Raises:
        TypeError: A callback, opened source, identity, cancellation capability, or
            transformed event violates the AG-UI source contract.
        ValueError: The opened stream or an event publishes a different run identity
            or changes an established protocol contract.
        BaseException: Owner setup or source cleanup fails.
    """

    resolved_identity = required_identity(identity)
    if not callable(open_events):
        raise TypeError("open_events must be an async callable")
    if transform_event is not None and not callable(transform_event):
        raise TypeError("transform_event must be a callable or None")

    async def opener() -> MessageSourceBinding[BaseEvent]:
        pending = open_events(resolved_identity)
        if not inspect.isawaitable(pending):
            raise TypeError("open_events must return an awaitable")
        opened = await pending
        if not isinstance(opened, MessageSource):
            raise TypeError("open_events must resolve to a MessageSource")
        source = opened
        opened_object = cast(object, opened)
        try:
            stream_identity = getattr(opened_object, "messaging_identity", None)
            if stream_identity is not resolved_identity:
                if not isinstance(stream_identity, RunIdentity):
                    raise TypeError("opened AG-UI source has no RunIdentity")
                raise ValueError(
                    "opened AG-UI source must retain the supplied identity object"
                )
            waits_for_first = getattr(
                opened_object,
                "messaging_cancel_waits_for_first_item",
                None,
            )
            if waits_for_first is not True:
                raise TypeError(
                    "opened AG-UI source must wait for its first item before cancellation"
                )
            cancel = getattr(opened_object, "messaging_cancel_callback", None)
            if not callable(cancel):
                raise TypeError("opened AG-UI source must publish cancellation")
            event_transform = transform_event

            async def transform(event: BaseEvent) -> BaseEvent:
                expected_type = _protocol_model_type(event)
                expected_identity = _event_protocol_identity(event)
                expected_metadata = _interrupt_metadata(event)
                if event_transform is None:
                    value = event
                else:
                    value = event_transform(event)
                    if inspect.isawaitable(value):
                        value = await value
                validated = _EVENT_ADAPTER.validate_python(value)
                if _protocol_model_type(validated) is not expected_type:
                    raise TypeError(
                        "transform_event must preserve the AG-UI event type"
                    )
                # AG-UI models allow extra product fields. The shared codec boundary
                # prevents a duplicate alias from hiding behind an unchanged Python
                # field until persistence.
                transformed = _canonical_event(
                    validated,
                    expected_type=expected_type,
                    expected_identity=expected_identity,
                    expected_interrupt_metadata=expected_metadata,
                )
                _validate_event_run_identity(transformed, resolved_identity)
                return transformed

            return MessageSourceBinding(source=map_source(source, transform))
        except BaseException as error:
            try:
                await source.aclose()
            except BaseException as close_error:
                if isinstance(error, Exception) and not isinstance(
                    close_error,
                    Exception,
                ):
                    close_error.add_note(
                        "Rejected AG-UI source validation also failed: "
                        f"{type(error).__name__}: {error}"
                    )
                    raise close_error from error
                error.add_note(
                    "Rejected AG-UI source cleanup also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            raise

    return ProfiledDeferredMessageSource(
        opener,
        identity=resolved_identity,
        codec_profile="agui.event",
        source_type=BaseEvent,
        replay_type=BaseEvent,
        cancellable=True,
        cancel_after_first_item=True,
    )


class AgUiCodec(
    MessageCodec[BaseEvent, BaseEvent],
    SseRenderer[BaseEvent],
):
    """Persist complete AG-UI events under a schema-stable codec identifier."""

    codec_id: ClassVar[str] = "agui.event"
    messaging_source_type: ClassVar[type[BaseEvent]] = BaseEvent
    messaging_replay_type: ClassVar[type[BaseEvent]] = BaseEvent

    def encode(self, item: BaseEvent) -> bytes:
        """Validate and encode one event with protocol field aliases."""

        event = _EVENT_ADAPTER.validate_python(item)
        canonical = _canonical_event(
            event,
            expected_type=_protocol_model_type(event),
            expected_identity=_event_protocol_identity(event),
            expected_interrupt_metadata=_interrupt_metadata(event),
        )
        return canonical.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode()

    def decode(self, payload: bytes) -> BaseEvent:
        """Decode and validate one complete discriminated AG-UI event."""

        return _EVENT_ADAPTER.validate_json(payload)

    def render(self, *, seq: int, payload: BaseEvent) -> bytes:
        """Render the durable sequence and native AG-UI JSON as one SSE frame."""

        encoded = self.encode(payload)
        return b"id: " + str(seq).encode() + b"\ndata: " + encoded + b"\n\n"
