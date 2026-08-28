from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar, Generic, Never, TypeVar, assert_type, cast

import pytest
from pydantic import BaseModel

import tinkerfin_messaging
from tinkerfin import (
    NativeStreamPart,
    RunIdentity,
    TinkerFin,
)
from tinkerfin_messaging import (
    BackendRunHandle,
    CodecMismatch,
    MemoryBackend,
    MessageCodecInputSource,
    MessageEnvelope,
    MessageSource,
    MessageSubscription,
    Messaging,
    MessagingError,
    PreparedRun,
    RecoveryCheckpoint,
)
from tinkerfin_messaging.protocols import ProfiledMessageSource

CollectedT = TypeVar("CollectedT")
SourceValueT = TypeVar("SourceValueT")


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(threadId=thread_id, runId=run_id)


class _TextCodec:
    codec_id: ClassVar[str] = "test.text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _TextRenderer:
    def render(self, *, seq: int, payload: str) -> bytes:
        return f"id: {seq}\ndata: {payload}\n\n".encode()


@dataclass(frozen=True, slots=True)
class _DataclassValue:
    value: int


class _ModelValue(BaseModel):
    value: int


class _DataclassCodec:
    codec_id: ClassVar[str] = "test.dataclass.v1"

    def encode(self, item: _DataclassValue) -> bytes:
        return str(item.value).encode()

    def decode(self, payload: bytes) -> _DataclassValue:
        return _DataclassValue(value=int(payload))


class _ModelCodec:
    codec_id: ClassVar[str] = "test.model.v1"

    def encode(self, item: _ModelValue) -> bytes:
        return item.model_dump_json().encode()

    def decode(self, payload: bytes) -> _ModelValue:
        return _ModelValue.model_validate_json(payload)


class _CountingBackend(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.prepare_calls = 0
        self.append_calls = 0

    async def prepare(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun:
        self.prepare_calls += 1
        return await super().prepare(
            channel=channel,
            identity=identity,
            codec=codec,
            after=after,
            cancellable=cancellable,
            recoverable=recoverable,
        )

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        self.append_calls += 1
        return await super().append(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )


class _DeclaredProfileSource:
    messaging_source_type: type[object]
    messaging_codec_input_type: type[object]
    messaging_replay_type: type[object]

    def __init__(
        self,
        *,
        profile: str,
        source_type: type[object] | None = None,
        codec_input_type: type[object] | None = None,
        replay_type: type[object] | None = None,
        identity: RunIdentity | None = None,
    ) -> None:
        self.messaging_codec_profile = profile
        self.messaging_identity = identity or _identity()
        if source_type is not None:
            self.messaging_source_type = source_type
        if codec_input_type is not None:
            self.messaging_codec_input_type = codec_input_type
        if replay_type is not None:
            self.messaging_replay_type = replay_type
        self.iterator_calls = 0
        self.pull_calls = 0
        self.close_calls = 0
        self.transform_calls = 0

    def __aiter__(self) -> _DeclaredProfileSource:
        self.iterator_calls += 1
        return self

    async def __anext__(self) -> Mapping[str, object]:
        self.pull_calls += 1
        raise StopAsyncIteration

    def messaging_codec_input(
        self,
        item: Mapping[str, object],
    ) -> NativeStreamPart:
        self.transform_calls += 1
        return NativeStreamPart.model_validate(item)

    async def aclose(self) -> None:
        self.close_calls += 1


class _CustomSource:
    def __init__(self, *items: str) -> None:
        self._items = iter(items)
        self.closed = asyncio.Event()

    def __aiter__(self) -> _CustomSource:
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed.set()


class _UnrelatedProfileTextSource(_CustomSource):
    messaging_codec_profile: ClassVar[str] = "vendor.unrelated.v1"
    messaging_source_type: ClassVar[type[int]] = int
    messaging_replay_type: ClassVar[type[int]] = int


class _ObjectSource(Generic[SourceValueT]):
    def __init__(
        self,
        *items: SourceValueT,
        on_pull: Callable[[], None] | None = None,
    ) -> None:
        self._items = iter(items)
        self._on_pull = on_pull

    def __aiter__(self) -> _ObjectSource[SourceValueT]:
        return self

    async def __anext__(self) -> SourceValueT:
        if self._on_pull is not None:
            self._on_pull()
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        return None


class _NativeProfileSource:
    def __init__(
        self,
        value: int | None,
        *,
        identity: RunIdentity,
    ) -> None:
        self.messaging_identity = identity
        self._value = value
        self._yielded = False

    @property
    def messaging_codec_profile(self) -> str:
        return "tinkerfin.native-stream"

    @property
    def messaging_source_type(self) -> type[Mapping[str, object]]:
        return Mapping

    @property
    def messaging_codec_input_type(self) -> type[NativeStreamPart]:
        return NativeStreamPart

    @property
    def messaging_replay_type(self) -> type[NativeStreamPart]:
        return NativeStreamPart

    def messaging_codec_input(
        self,
        item: Mapping[str, object],
    ) -> NativeStreamPart:
        return NativeStreamPart.model_validate(item)

    def __aiter__(self) -> _NativeProfileSource:
        return self

    async def __anext__(self) -> Mapping[str, object]:
        if self._yielded or self._value is None:
            raise StopAsyncIteration
        self._yielded = True
        return {
            "type": "values",
            "ns": (),
            "data": {"value": self._value},
            "interrupts": (),
        }

    async def aclose(self) -> None:
        self._yielded = True


def _native_source(
    value: int = 1,
    *,
    identity: RunIdentity | None = None,
) -> _NativeProfileSource:
    return _NativeProfileSource(value, identity=identity or _identity())


def _empty_native_source(*, identity: RunIdentity) -> _NativeProfileSource:
    return _NativeProfileSource(None, identity=identity)


async def _collect(
    subscription: MessageSubscription[CollectedT],
) -> list[CollectedT]:
    return [message.data async for message in subscription]


@pytest.mark.asyncio
async def test_generic_dataclass_and_model_streams_require_explicit_codecs() -> None:
    dataclass_source = _ObjectSource(_DataclassValue(value=1))
    model_source = _ObjectSource(_ModelValue(value=2))

    assert not isinstance(dataclass_source, ProfiledMessageSource)
    assert not isinstance(model_source, ProfiledMessageSource)

    async with Messaging() as messaging:
        dataclass_subscription = await messaging.channel(
            name="dataclass",
            codec=_DataclassCodec(),
        ).wrap(
            dataclass_source,
            identity=_identity(thread_id="stream-1"),
            after=0,
        )
        model_subscription = await messaging.channel(
            name="model",
            codec=_ModelCodec(),
        ).wrap(
            model_source,
            identity=_identity(thread_id="stream-1"),
            after=0,
        )

        assert [message.data async for message in dataclass_subscription] == [
            _DataclassValue(value=1)
        ]
        assert [message.data async for message in model_subscription] == [
            _ModelValue(value=2)
        ]


@pytest.mark.asyncio
async def test_native_profile_infers_live_and_replay_types() -> None:
    source = _native_source()

    assert isinstance(source, ProfiledMessageSource)
    assert isinstance(source, MessageCodecInputSource)
    assert source.messaging_codec_profile == "tinkerfin.native-stream"
    assert source.messaging_source_type is Mapping
    assert source.messaging_codec_input_type is NativeStreamPart
    assert source.messaging_replay_type is NativeStreamPart

    async with Messaging() as messaging:
        subscription = await messaging.channel(name="native").wrap(
            source,
            after=0,
        )
        assert_type(subscription, MessageSubscription[NativeStreamPart])
        replay = [message.data async for message in subscription]

    assert len(replay) == 1
    assert isinstance(replay[0], NativeStreamPart)


@pytest.mark.asyncio
async def test_empty_native_profile_infers_identity_without_a_first_item() -> None:
    identity = _identity(thread_id="empty-thread", run_id="empty-run")

    async with Messaging() as messaging:
        subscription = await messaging.channel(name="native-empty").wrap(
            _empty_native_source(identity=identity),
            after=0,
        )

        assert [message async for message in subscription] == []


@pytest.mark.asyncio
async def test_generic_object_stream_without_codec_fails_before_prepare() -> None:
    factory_calls = 0

    def record_pull() -> None:
        nonlocal factory_calls
        factory_calls += 1

    backend = _CountingBackend()
    source = _ObjectSource(_DataclassValue(value=1), on_pull=record_pull)
    async with Messaging(backend=backend) as messaging:
        with pytest.raises(TypeError, match="provide codec explicitly"):
            await messaging.channel(name="objects").wrap(
                cast(MessageSource[Never], source),
                identity=_identity(thread_id="stream-1"),
            )

    assert backend.prepare_calls == 0
    assert backend.append_calls == 0
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_custom_source_requires_one_explicit_identity_before_prepare() -> None:
    backend = _CountingBackend()
    source = _CustomSource("value")

    async with Messaging(backend=backend) as messaging:
        untyped_wrap = cast(
            Callable[..., Awaitable[object]],
            messaging.channel(name="custom", codec=_TextCodec()).wrap,
        )
        with pytest.raises(TypeError, match="provide identity explicitly"):
            await untyped_wrap(source)

    assert backend.prepare_calls == 0
    assert source.closed.is_set()


@pytest.mark.asyncio
async def test_explicit_identity_must_match_profiled_source_before_prepare() -> None:
    backend = _CountingBackend()
    source = _native_source(
        identity=_identity(thread_id="thread-source", run_id="run-source")
    )

    async with Messaging(backend=backend) as messaging:
        with pytest.raises(ValueError, match="must match source messaging_identity"):
            await messaging.channel(name="native").wrap(
                source,
                identity=_identity(thread_id="thread-other", run_id="run-other"),
            )

    assert backend.prepare_calls == 0
    assert backend.append_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "codec_input_type", "replay_type", "reason"),
    [
        (None, NativeStreamPart, NativeStreamPart, "missing live source type"),
        (Mapping, None, NativeStreamPart, "missing codec input type"),
        (Mapping, str, NativeStreamPart, "codec input type"),
        (Mapping, NativeStreamPart, str, "replay type"),
    ],
)
async def test_incomplete_native_profile_fails_before_prepare(
    source_type: type[object] | None,
    codec_input_type: type[object] | None,
    replay_type: type[object] | None,
    reason: str,
) -> None:
    backend = _CountingBackend()
    source = _DeclaredProfileSource(
        profile="tinkerfin.native-stream",
        source_type=source_type,
        codec_input_type=codec_input_type,
        replay_type=replay_type,
    )
    mismatch_type = getattr(
        tinkerfin_messaging,
        "SourceProfileMismatch",
        MessagingError,
    )

    async with Messaging(backend=backend) as messaging:
        with pytest.raises(mismatch_type, match=reason) as captured:
            await messaging.channel(name="native").wrap(
                source,
            )

    assert type(captured.value).__name__ == "SourceProfileMismatch"
    assert backend.prepare_calls == 0
    assert backend.append_calls == 0
    assert source.iterator_calls == 0
    assert source.pull_calls == 0
    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_unknown_profile_fails_without_opening_the_source_or_backend() -> None:
    backend = _CountingBackend()
    source = _DeclaredProfileSource(
        profile="vendor.unknown.v1",
        source_type=Mapping,
        codec_input_type=NativeStreamPart,
        replay_type=NativeStreamPart,
    )

    async with Messaging(backend=backend) as messaging:
        with pytest.raises(TypeError, match="unsupported built-in codec profile"):
            await messaging.channel(name="native").wrap(
                source,
            )

    assert backend.prepare_calls == 0
    assert backend.append_calls == 0
    assert source.iterator_calls == 0
    assert source.pull_calls == 0
    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_reused_inferred_channel_revalidates_profile_before_prepare() -> None:
    backend = _CountingBackend()
    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="native")
        first = await channel.wrap(
            _native_source(identity=_identity(thread_id="stream-1")),
            after=0,
        )
        assert len([message async for message in first]) == 1
        baseline_prepare = backend.prepare_calls
        baseline_append = backend.append_calls

        malformed = _DeclaredProfileSource(
            profile="tinkerfin.native-stream",
            source_type=Mapping,
            codec_input_type=str,
            replay_type=NativeStreamPart,
        )
        with pytest.raises(
            tinkerfin_messaging.SourceProfileMismatch,
            match="codec input type",
        ):
            await channel.wrap(
                malformed,
            )

    assert backend.prepare_calls == baseline_prepare
    assert backend.append_calls == baseline_append
    assert malformed.iterator_calls == 0
    assert malformed.pull_calls == 0
    assert malformed.close_calls == 1


@pytest.mark.asyncio
async def test_name_only_channel_infers_native_and_agui_profiles() -> None:
    async with Messaging() as messaging:
        native_channel = messaging.channel(name="native-parts")
        native_frames = await native_channel.sse(
            _native_source(
                identity=_identity(thread_id="native-stream", run_id="native-run")
            ),
            after=0,
        )
        native = [frame async for frame in native_frames]

        agui_channel = messaging.channel(name="agui-events")
        agui_identity = _identity()
        events = TinkerFin().failed_agui_run(
            RuntimeError("test initialization failure"),
            identity=agui_identity,
        )
        agui_frames = await agui_channel.sse(
            events,
            after=0,
        )
        agui = [frame async for frame in agui_frames]
        committed_agui = await agui_channel.read(
            identity=agui_identity,
            after=0,
            limit=100,
        )

    assert native[0].startswith(b"id: 1\nevent: stream-part\ndata: ")
    assert json.loads(native[0].split(b"data: ", 1)[1])["type"] == "values"
    assert [json.loads(frame.split(b"data: ", 1)[1])["type"] for frame in agui] == [
        "RUN_STARTED",
        "RUN_ERROR",
    ]
    assert [message.data.type.value for message in committed_agui] == [
        "RUN_STARTED",
        "RUN_ERROR",
    ]


@pytest.mark.asyncio
async def test_name_only_channel_handle_is_reusable_across_streams() -> None:
    async with Messaging() as messaging:
        channel = messaging.channel(name="native-parts")
        first = await channel.wrap(
            _native_source(1, identity=_identity(thread_id="stream-1")),
            after=0,
        )
        second = await channel.wrap(
            _native_source(
                2,
                identity=_identity(thread_id="stream-2", run_id="run-2"),
            ),
            after=0,
        )

        first_parts, second_parts = await asyncio.gather(
            _collect(first),
            _collect(second),
        )

    first_part = first_parts[0]
    second_part = second_parts[0]
    assert isinstance(first_part, NativeStreamPart)
    assert isinstance(second_part, NativeStreamPart)
    assert first_part.data == {"value": 1}
    assert second_part.data == {"value": 2}


@pytest.mark.asyncio
async def test_name_only_channel_rejects_custom_and_incompatible_sources() -> None:
    custom = _CustomSource("value")

    async with Messaging() as messaging:
        channel = messaging.channel(name="events")
        with pytest.raises(TypeError, match="does not advertise a built-in codec"):
            await channel.wrap(
                cast(MessageSource[Never], custom),
                identity=_identity(thread_id="custom", run_id="custom-run"),
            )
        native = await channel.wrap(
            _native_source(identity=_identity(thread_id="native", run_id="native-run")),
            after=0,
        )
        await _collect(native)

        incompatible = TinkerFin().failed_agui_run(
            RuntimeError("test initialization failure"),
            identity=_identity(thread_id="agui", run_id="agui-run"),
        )
        with pytest.raises(CodecMismatch):
            await channel.wrap(
                incompatible,
                after=0,
            )

    assert custom.closed.is_set()


@pytest.mark.asyncio
async def test_preencoded_runtime_sse_is_rejected_before_source_open() -> None:
    encoded = (
        TinkerFin()
        .failed_agui_run(
            RuntimeError("test initialization failure"),
            identity=_identity(thread_id="stream-1"),
        )
        .to_sse()
    )
    async with Messaging() as messaging:
        channel = messaging.channel(name="native-parts")
        with pytest.raises(TypeError, match="object events, not pre-encoded SSE"):
            await channel.wrap(
                cast(MessageSource[Never], encoded),
                identity=_identity(thread_id="stream-1"),
            )


@pytest.mark.asyncio
async def test_explicit_custom_codec_and_renderer_remain_supported() -> None:
    source = _UnrelatedProfileTextSource("one", "two")
    async with Messaging() as messaging:
        channel = messaging.channel(
            name="custom",
            codec=_TextCodec(),
            renderer=_TextRenderer(),
        )
        subscription = await channel.wrap(
            source,
            identity=_identity(thread_id="stream-1"),
            after=0,
        )
        assert_type(subscription, MessageSubscription[str])
        frames = subscription.sse()
        assert [frame async for frame in frames] == [
            b"id: 1\ndata: one\n\n",
            b"id: 2\ndata: two\n\n",
        ]
    assert source.closed.is_set()


@pytest.mark.asyncio
async def test_channel_sse_matches_wrap_then_subscription_sse() -> None:
    async with Messaging() as messaging:
        direct_channel = messaging.channel(
            name="direct",
            codec=_TextCodec(),
            renderer=_TextRenderer(),
        )
        direct_body = await direct_channel.sse(
            _CustomSource("one", "two"),
            identity=_identity(thread_id="stream-1"),
            after=0,
        )
        direct = [frame async for frame in direct_body]

        composed_channel = messaging.channel(
            name="composed",
            codec=_TextCodec(),
            renderer=_TextRenderer(),
        )
        subscription = await composed_channel.wrap(
            _CustomSource("one", "two"),
            identity=_identity(thread_id="stream-1"),
            after=0,
        )
        composed = [frame async for frame in subscription.sse()]

    assert direct == composed
