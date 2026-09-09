"""Explicit Deep Agents integration profiles selected before a Runtime is created."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast, runtime_checkable

from anyio import to_thread
from deepagents import graph as _deepagents_graph
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.graph.state import CompiledStateGraph

from ._hitl import prepare_hitl_factory_overrides
from ._v3_stream import graph_v3_stream
from .native_driver import (
    DeepAgentsV2StreamDriver,
    DeepAgentsV3StreamDriver,
    NativeStreamDriver,
    ReasoningExtractor,
)

_DEEP_AGENTS_CREATE_FACTORY = cast(
    Callable[..., object],
    _deepagents_graph.create_deep_agent,  # pyright: ignore[reportUnknownMemberType]
)
_DEEP_AGENTS_CREATE_SIGNATURE = inspect.signature(_DEEP_AGENTS_CREATE_FACTORY)
_COMPILED_ASTREAM = cast(
    Callable[..., object],
    cast(
        object,
        CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType]
    ),
)
_COMPILED_ASTREAM_SIGNATURE = inspect.signature(_COMPILED_ASTREAM).replace(
    parameters=tuple(inspect.signature(_COMPILED_ASTREAM).parameters.values())[1:]
)

# LangGraph 1.2.10's PregelLoop._first() applies writes owned by this task before
# preparing interrupted tasks, while map_command() persists native decisions on the
# resume channel. These locked values belong to the v2 Profile rather than TinkerFin's
# internal protocol; the resume settlement and Redis saver contract tests guard them.
_V2_NULL_TASK_ID = "00000000-0000-0000-0000-000000000000"
_V2_RESUME_CHANNEL = "__resume__"

_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


@dataclass(frozen=True, slots=True)
class DeepAgentsFactoryPreparation:
    """Describe Profile-owned changes to one current agent factory call.

    Attributes:
        keyword_overrides: Canonical keyword values that replace the caller's values
            before the selected graph factory is invoked.
        uncontracted_external_subagents: External compiled subagent names that cannot
            participate in TinkerFin's mixed-cancellation extension.
    """

    keyword_overrides: Mapping[str, object]
    uncontracted_external_subagents: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Snapshot mutable mappings and reject ambiguous override names."""

        if not isinstance(self.keyword_overrides, Mapping):
            raise TypeError("keyword_overrides must be a mapping")
        overrides = dict(self.keyword_overrides)
        if any(
            not isinstance(name, str) or not name or name != name.strip()
            for name in overrides
        ):
            raise ValueError("factory override names must be canonical text")
        if not isinstance(self.uncontracted_external_subagents, frozenset):
            raise TypeError("uncontracted_external_subagents must be a frozenset")
        object.__setattr__(self, "keyword_overrides", MappingProxyType(overrides))


@runtime_checkable
class DeepAgentsRuntimeProfile(Protocol):
    """Provide one complete Deep Agents build and Native stream integration.

    Hosts select a concrete Profile before creating a Definition. One TinkerFin
    instance and every Run created from it retain that exact Profile; the Runtime never
    negotiates or infers a profile from stream data. The same Profile owns the upstream
    saver semantics used to stage a resume without discarding pending root or subgraph
    work. ``profile_id`` identifies a third-party integration implementation, not a
    TinkerFin protocol version.

    A Profile whose graph factory is asynchronous may additionally implement
    ``create_agent_graph(factory, args, kwargs)``. Profiles that expose only this base
    contract keep their selected synchronous factory; Core invokes it in AnyIO's
    capacity-limited worker boundary. Both paths preserve the Profile's own factory and
    never fall back to another integration.
    """

    @property
    def profile_id(self) -> str:
        """Return the canonical host-visible integration identity."""

        ...

    @property
    def create_agent_factory(self) -> Callable[..., object]:
        """Return the concrete Deep Agents graph factory owned by this Profile.

        A Profile may return an adapter around a different upstream release, but that
        adapter must honor ``create_agent_signature`` so TinkerFin can apply current
        state, HITL, and Plan contracts before invocation.

        Returns:
            Concrete callable implementing ``create_agent_signature``.
        """

        ...

    @property
    def create_agent_signature(self) -> inspect.Signature:
        """Return the stable build signature implemented by the Profile factory."""

        ...

    @property
    def astream_signature(self) -> inspect.Signature:
        """Return the stable bound Graph stream signature before graph construction."""

        ...

    def graph_stream(self, graph: object) -> Callable[..., object]:
        """Return the concrete event source callable for one created Graph."""

        ...

    def prepare_create_agent(
        self,
        arguments: Mapping[str, object],
    ) -> DeepAgentsFactoryPreparation:
        """Adapt one bound current build call to this Profile's upstream contract."""

        ...

    @property
    def stream_driver(self) -> NativeStreamDriver:
        """Return the immutable invocation and stream normalization Driver."""

        ...

    async def stage_resume_intent(
        self,
        checkpointer: _CheckpointSaver,
        config: RunnableConfig,
        writes: tuple[tuple[str, object], ...],
    ) -> None:
        """Persist private state writes without changing interrupted Graph control.

        Args:
            checkpointer: Borrowed saver that owns the interrupted checkpoint.
            config: Exact checkpoint configuration selected by Runtime lineage.
            writes: Ordered private lineage and marker values.

        Raises:
            Exception: The concrete Profile cannot durably establish this intent.
        """

        ...

    def pending_resume_values(
        self,
        checkpoint: CheckpointTuple,
        *,
        channel_name: str,
    ) -> tuple[object, ...]:
        """Return Profile-owned pending private values from one checkpoint.

        Args:
            checkpoint: Saver tuple at the canonical thread head.
            channel_name: TinkerFin private state channel to inspect.

        Returns:
            Immutable raw values that the Runtime validates as exact markers.
        """

        ...

    def native_resume_submitted(self, checkpoint: CheckpointTuple) -> bool:
        """Return whether the Profile finds a durably submitted native decision.

        Args:
            checkpoint: Saver tuple at the canonical thread head.

        Returns:
            ``True`` only when retry must not submit the decision again.
        """

        ...


class DeepAgentsV2RuntimeProfile:
    """Integrate the locked Deep Agents 0.7.5 and LangGraph v2 runtime.

    This is the default stable Runtime Profile. Its factory signature, HITL preparation,
    invocation binding, checkpoint identity, resume-intent staging, and frame
    normalization are jointly exercised by the Runtime Profile and lineage contract
    tests.

    Args:
        reasoning_extractors: Verified provider-specific reasoning extractors applied
            while the v2 Driver normalizes live messages. An empty tuple emits no
            reasoning observations.

    Raises:
        TypeError: A reasoning extractor does not implement ``ReasoningExtractor``.
        ValueError: Reasoning extractor names are invalid or duplicated.
    """

    def __init__(
        self,
        *,
        reasoning_extractors: tuple[ReasoningExtractor, ...] = (),
    ) -> None:
        """Create one request-independent v2 Driver from verified extractors."""

        self._stream_driver = DeepAgentsV2StreamDriver(
            reasoning_extractors=reasoning_extractors,
        )

    @property
    def profile_id(self) -> str:
        """Return the concrete third-party integration identity."""

        return "deepagents-v2"

    @property
    def create_agent_factory(self) -> Callable[..., object]:
        """Return the currently installed locked factory without invoking it.

        Resolving the symbol on access keeps dependency monkeypatching and process-local
        instrumentation scoped to Definition creation rather than Profile construction.

        Returns:
            Locked Deep Agents graph factory currently installed in this process.
        """

        return cast(
            Callable[..., object],
            _deepagents_graph.create_deep_agent,  # pyright: ignore[reportUnknownMemberType]
        )

    @property
    def create_agent_signature(self) -> inspect.Signature:
        """Return the locked factory contract independent of process instrumentation."""

        return _DEEP_AGENTS_CREATE_SIGNATURE

    @property
    def astream_signature(self) -> inspect.Signature:
        """Return the locked bound LangGraph stream contract used by lazy resume."""

        return _COMPILED_ASTREAM_SIGNATURE

    def graph_stream(self, graph: object) -> Callable[..., object]:
        """Return the locked graph's v2 ``astream`` callable."""

        stream = getattr(graph, "astream", None)
        if not callable(stream):
            raise TypeError("Deep Agents v2 graph must expose astream")
        return stream

    def prepare_create_agent(
        self,
        arguments: Mapping[str, object],
    ) -> DeepAgentsFactoryPreparation:
        """Apply the locked Deep Agents HITL and permission ownership contract.

        Args:
            arguments: Factory arguments already bound to
                ``create_agent_signature`` with defaults applied.

        Returns:
            Immutable overrides for the selected factory and the external subagent
            cancellation boundary.
        """

        hitl = prepare_hitl_factory_overrides(arguments)
        return DeepAgentsFactoryPreparation(
            keyword_overrides={
                "interrupt_on": hitl.interrupt_on,
                "middleware": hitl.middleware,
                "permissions": hitl.permissions,
                "subagents": hitl.subagents,
            },
            uncontracted_external_subagents=(hitl.uncontracted_external_subagents),
        )

    async def create_agent_graph(
        self,
        factory: Callable[..., object],
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> object:
        """Build the locked synchronous Deep Agents graph in a bounded worker.

        AnyIO's process-wide worker limiter supplies the required capacity bound. The
        default non-abandoning cancellation behavior keeps the synchronous build owned
        until it settles, so cancellation cannot leave an unobserved graph construction
        running after this method returns.

        Args:
            factory: Exact factory captured by the Definition.
            args: Frozen positional arguments for that factory.
            kwargs: Frozen keyword arguments for that factory.

        Returns:
            The freshly constructed locked Deep Agents graph.
        """

        build = partial(factory, *args, **dict(kwargs))
        return await to_thread.run_sync(build)

    @property
    def stream_driver(self) -> NativeStreamDriver:
        """Return the request-independent v2 stream Driver."""

        return self._stream_driver

    async def stage_resume_intent(
        self,
        checkpointer: _CheckpointSaver,
        config: RunnableConfig,
        writes: tuple[tuple[str, object], ...],
    ) -> None:
        """Persist v2 null-task writes while preserving the interrupted checkpoint.

        ``BaseCheckpointSaver.aput_writes()`` is awaited before host settlement. The
        Graph later applies these writes in the same invocation that consumes the
        native resume decision, so root, Planning, and nested subgraph task state remain
        unchanged during the callback window. Matching partial writes are completed on
        retry; conflicting or unrelated null-task writes fail closed.

        Args:
            checkpointer: Borrowed saver that owns the exact interrupted checkpoint.
            config: Exact root checkpoint configuration selected by lineage validation.
            writes: Ordered private lineage and marker channel values.

        Raises:
            TypeError: A write does not use a canonical channel name.
            ValueError: Existing Profile-owned writes conflict with this intent.
        """

        if any(
            not isinstance(channel, str) or not channel or channel != channel.strip()
            for channel, _value in writes
        ):
            raise TypeError("resume intent channels must be canonical text")
        expected = dict(writes)
        if len(expected) != len(writes):
            raise ValueError("resume intent channels must be unique")
        checkpoint = await checkpointer.aget_tuple(config)
        if checkpoint is None:
            raise ValueError("resume intent checkpoint is unavailable")
        existing: dict[str, object] = {}
        for task_id, channel, value in checkpoint.pending_writes or ():
            if task_id != _V2_NULL_TASK_ID or channel == _V2_RESUME_CHANNEL:
                continue
            if channel not in expected:
                raise ValueError(
                    "checkpoint contains an unrelated v2 null-task state write"
                )
            if channel in existing and existing[channel] != value:
                raise ValueError(
                    "checkpoint contains conflicting v2 null-task state writes"
                )
            existing[channel] = value
        if any(expected[channel] != value for channel, value in existing.items()):
            raise ValueError("checkpoint contains a different resume intent")
        if existing == expected:
            return
        await checkpointer.aput_writes(config, writes, _V2_NULL_TASK_ID)

    def pending_resume_values(
        self,
        checkpoint: CheckpointTuple,
        *,
        channel_name: str,
    ) -> tuple[object, ...]:
        """Return pending private writes owned by the v2 null task.

        Args:
            checkpoint: Saver tuple at the canonical thread head.
            channel_name: Private state channel selected by TinkerFin.

        Returns:
            Immutable raw marker values pending at the locked v2 boundary.
        """

        return tuple(
            value
            for task_id, channel, value in checkpoint.pending_writes or ()
            if task_id == _V2_NULL_TASK_ID and channel == channel_name
        )

    def native_resume_submitted(self, checkpoint: CheckpointTuple) -> bool:
        """Return whether v2 persisted a native resume write at this checkpoint.

        Args:
            checkpoint: Saver tuple at the canonical thread head.

        Returns:
            Whether the locked v2 resume channel has a durable pending write.
        """

        return any(
            channel == _V2_RESUME_CHANNEL
            for _task_id, channel, _value in checkpoint.pending_writes or ()
        )

    def _matches_resume_subgraph_namespace(
        self,
        namespace: tuple[str, ...],
        pending_task_ids: frozenset[str],
    ) -> bool:
        """Identify dynamic v2 subgraphs owned by currently interrupted root tasks.

        Deep Agents 0.7.5 names an ordinary dynamic child ``tools:<task-id>`` and
        appends a decimal slot for parallel children. Nested children retain that first
        component. This private Profile hook keeps the upstream convention out of Core
        and does not add a required method to existing custom Runtime Profiles.
        """

        if not namespace or not pending_task_ids:
            return False
        first = namespace[0]
        for task_id in pending_task_ids:
            prefix = f"tools:{task_id}"
            if first == prefix:
                return True
            if first.startswith(f"{prefix}:") and first[len(prefix) + 1 :].isdecimal():
                return True
        return False


class DeepAgentsV3RuntimeProfile(DeepAgentsV2RuntimeProfile):
    """Integrate the locked experimental LangGraph v3 event-stream API.

    The installed LangGraph 1.2.10 implementation still drives its v3 mux from an
    internal v2 stream. Selecting this Profile nevertheless exercises the public
    ``astream_events(version="v3")`` contract and never falls back to the v2 Profile.

    Args:
        reasoning_extractors: Verified provider reasoning extractors applied after v3
            events enter TinkerFin's canonical Native boundary.
    """

    def __init__(
        self,
        *,
        reasoning_extractors: tuple[ReasoningExtractor, ...] = (),
    ) -> None:
        """Create one request-independent v3 Driver from verified extractors."""

        self._stream_driver = DeepAgentsV3StreamDriver(
            reasoning_extractors=reasoning_extractors,
        )

    @property
    def profile_id(self) -> str:
        """Return the explicit experimental integration identity."""

        return "deepagents-v3"

    def graph_stream(self, graph: object) -> Callable[..., object]:
        """Return a canonical source carried by public v3 protocol events."""

        return graph_v3_stream(graph)


__all__ = [
    "DeepAgentsFactoryPreparation",
    "DeepAgentsRuntimeProfile",
    "DeepAgentsV2RuntimeProfile",
    "DeepAgentsV3RuntimeProfile",
]
