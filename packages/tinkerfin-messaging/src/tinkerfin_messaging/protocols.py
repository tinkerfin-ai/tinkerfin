"""Structural extension boundaries for sources, codecs, and SSE renderers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from .models import RecoverableMessage, RecoveryCheckpoint

SourceT_co = TypeVar("SourceT_co", covariant=True)
SourceT_contra = TypeVar("SourceT_contra", contravariant=True)
ReplayT_co = TypeVar("ReplayT_co", covariant=True)
ReplayT_contra = TypeVar("ReplayT_contra", contravariant=True)
RecoverableSourceT = TypeVar("RecoverableSourceT")


@runtime_checkable
class MessageSource(Protocol[SourceT_co]):
    """Single-use asynchronous source whose close operation is idempotent."""

    def __aiter__(self) -> AsyncIterator[SourceT_co]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ProfiledMessageSource(
    MessageSource[SourceT_co],
    Protocol[SourceT_co, ReplayT_co],
):
    """Object source with a complete immutable built-in codec profile.

    The live type is consumed by the selected codec. The replay type is produced by
    decoding committed bytes and determines the subscription's static item type.
    """

    @property
    def messaging_codec_profile(self) -> str: ...

    @property
    def messaging_source_type(self) -> type[SourceT_co]: ...

    @property
    def messaging_replay_type(self) -> type[ReplayT_co]: ...


@runtime_checkable
class RecoverableSource(Protocol[RecoverableSourceT]):
    """Rebuild a single-use source from its last committed checkpoint."""

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> MessageSource[RecoverableMessage[RecoverableSourceT]]: ...


@runtime_checkable
class MessageCodec(Protocol[SourceT_contra, ReplayT_co]):
    """Translate source values to and from one stable persisted byte schema."""

    codec_id: ClassVar[str]

    def encode(self, item: SourceT_contra) -> bytes: ...

    def decode(self, payload: bytes) -> ReplayT_co: ...


@runtime_checkable
class SseRenderer(Protocol[ReplayT_contra]):
    """Render one decoded payload with its durable sequence as an SSE frame."""

    def render(self, *, seq: int, payload: ReplayT_contra) -> bytes: ...
