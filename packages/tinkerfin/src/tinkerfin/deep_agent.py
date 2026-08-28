"""Deep Agents graph definitions and request-scoped runtimes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from deepagents import graph as _deepagents_graph
from deepagents.graph import DeepAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, StateSnapshot

from tinkerfin_contracts import RunIdentity

from ._agui_lineage_state import (
    LINEAGE_STATE_KEY,
    RESUME_MARKER_STATE_KEY,
    lineage_state_update,
)
from ._observation import source_context
from ._optional_dependencies import require_agui
from ._state_schema import compose_deep_agent_base_schema
from ._tasks import join_task
from .errors import TinkerFinLifecycleError
from .plan._config import (
    AgentMode,
    PlanOptions,
    resolve_agent_mode,
)

if TYPE_CHECKING:
    from .agui_resume import (
        AgUiResumeBinding,
        AgUiResumeCheckpointObserver,
        AgUiResumeInitializationFailureObserver,
        AgUiResumeRequest,
    )
    from .runtime import (
        AgUiEventStream,
        EventObserver,
        NativeGraphRunStream,
        PartObserver,
        TinkerFin,
    )

CreateP = ParamSpec("CreateP")
GraphT = TypeVar("GraphT")
AstreamT = TypeVar("AstreamT", bound=Callable[..., object])


class _GraphWithAstream(Protocol):
    astream: Callable[..., object]


class _PlanNativeGraph(Protocol):
    checkpointer: object

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot: ...

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: Mapping[str, object],
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig: ...


# The built-in factory supplies the public ParamSpec and generated-stub contract only.
# Every actual Definition build resolves the selected Profile's factory below, so this
# symbol must never become an invocation fallback for a custom Profile.
_public_create_agent_contract = cast(
    Callable[..., object],
    _deepagents_graph.create_deep_agent,  # pyright: ignore[reportUnknownMemberType]
)


def _graph_astream(graph: object) -> Callable[..., object]:
    """Read the callable stream boundary from one compiled upstream graph."""

    return cast(_GraphWithAstream, graph).astream


class _StreamClaim:
    __slots__ = ("_claimed",)

    def __init__(self) -> None:
        self._claimed = False

    def ensure_available(self) -> None:
        if self._claimed:
            raise TinkerFinLifecycleError(
                "a TinkerFin run can create only one object stream"
            )

    def claim(self) -> None:
        self.ensure_available()
        self._claimed = True


class _ResumeInitializationGuard:
    """Settle one host claim only while the resume marker is not durable.

    The guard is request-scoped and borrowed by the native stream's retained close
    task. Marking the guard prepared permanently fences release for that request;
    prepared and accepted retries must keep the host claim until the durable checkpoint
    callback completes. A pre-marker close, failure, or cancellation invokes the host
    callback at most once and waits for it independently of caller cancellation.
    """

    __slots__ = (
        "_callback",
        "_marker_probe",
        "_marker_readable",
        "_stage_started",
    )

    def __init__(
        self,
        callback: AgUiResumeInitializationFailureObserver | None,
        marker_probe: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        self._callback = callback
        self._marker_probe = marker_probe
        self._marker_readable = False
        self._stage_started = False

    def mark_stage_started(self) -> None:
        """Require a saver probe before releasing after a stage attempt."""

        self._stage_started = True

    def mark_marker_readable(self) -> None:
        """Prevent a durable prepared or accepted resume from being released."""

        self._marker_readable = True

    async def settle_unprepared(self) -> None:
        """Invoke and consume the host callback only before marker durability."""

        if self._marker_readable:
            return
        probe = self._marker_probe
        if self._stage_started and probe is not None and await probe():
            self._marker_readable = True
            return
        callback = self._callback
        self._callback = None
        if callback is None:
            return

        async def settle() -> None:
            await callback()

        task = asyncio.create_task(
            settle(),
            name="tinkerfin-resume-initialization-settlement",
        )
        await join_task(task)


def _wrap_native_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin,
    identity: RunIdentity,
    mode: AgentMode,
    private_state_keys: frozenset[str],
    on_part: PartObserver[Mapping[str, object]] | None,
) -> AstreamT:
    """Bind one Definition stream to its Profile, identity, and Observer lifecycle.

    Invocation validation happens before the single-use claim and before coordinator or
    Observer side effects. The wrapper preserves the installed callable signature while
    returning a TinkerFin-owned stream that keeps raw objects public and canonical
    frames private to downstream integrations.
    """

    signature = inspect.signature(astream)
    claim = _StreamClaim()

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> NativeGraphRunStream:
        bound = tinkerfin._bind_native_invocation(
            signature,
            args,
            kwargs,
            identity=identity,
        )
        claim.claim()
        graph_input = bound.arguments.get("input")
        raw_config = bound.arguments.get("config", {})
        input_kind = (
            "resume"
            if isinstance(graph_input, Command) and graph_input.resume is not None
            else "ordinary"
        )
        context = source_context(
            identity=identity,
            runtime_profile=tinkerfin.runtime_profile.profile_id,
            input_kind=input_kind,
            parent_run_id=None,
            mode=mode,
            graph_input=cast(object, graph_input),
            config=raw_config,
            private_state_keys=private_state_keys,
        )
        observation = tinkerfin._observation_hub(context)

        def source() -> AsyncIterator[Mapping[str, object]]:
            return cast(
                AsyncIterator[Mapping[str, object]],
                astream(*bound.args, **bound.kwargs),
            )

        return tinkerfin._run_native(
            source,
            identity=identity,
            on_part=on_part,
            observation=observation,
        )

    return cast(AstreamT, wrapped)


def _create_graph_agui_stream(
    *,
    native_astream_factory: Callable[
        [],
        Callable[..., AsyncIterator[Mapping[str, object]]],
    ],
    bound: inspect.BoundArguments,
    claim: _StreamClaim,
    tinkerfin: TinkerFin,
    identity: RunIdentity,
    parent_run_id: str | None,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    resume: AgUiResumeBinding | None,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_resume_initialization_failed: (AgUiResumeInitializationFailureObserver | None),
    on_event: EventObserver | None,
) -> AgUiEventStream:
    """Create one lazy AG-UI stream from an already bound Graph call."""

    require_agui()
    from ._agui_lineage import bind_agui_lineage, stage_agui_resume_intent

    claim.ensure_available()
    if resume is not None:
        durability = bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise TinkerFinLifecycleError("AG-UI resume requires durability='sync'")
    raw_config = bound.arguments.get("config", {})
    raw_input = (
        bound.arguments.get("input")
        if resume is None
        else resume.model_dump(mode="json", by_alias=True)
    )
    input_kind = (
        resume.mode
        if resume is not None
        else ("branch" if parent_run_id is not None else "ordinary")
    )
    context = source_context(
        identity=identity,
        runtime_profile=tinkerfin.runtime_profile.profile_id,
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        mode=mode,
        graph_input=raw_input,
        config=raw_config,
        private_state_keys=private_state_keys,
        resume=() if resume is None else resume._observation_summaries(),
    )
    observation = tinkerfin._observation_hub(context)
    resolved_native_astream: (
        Callable[..., AsyncIterator[Mapping[str, object]]] | None
    ) = None

    async def resume_marker_is_readable() -> bool:
        if resume is None:
            return False
        native_astream = resolved_native_astream
        if native_astream is None:
            return False
        raw_config = bound.arguments.get("config")
        if not isinstance(raw_config, Mapping):
            raise TypeError("bound Graph config must be a mapping")
        resolution = await bind_agui_lineage(
            native_astream,
            cast(RunnableConfig, raw_config),
            identity=identity,
            parent_run_id=parent_run_id,
            runtime_profile=tinkerfin.runtime_profile,
            resume=resume,
        )
        return resolution.resume_phase in {"prepared", "accepted"}

    resume_guard = _ResumeInitializationGuard(
        on_resume_initialization_failed,
        resume_marker_is_readable if resume is not None else None,
    )

    async def record_resume_checkpoint(effective_parent_run_id: str | None) -> None:
        assert resume is not None
        checkpoint = resume._checkpoint(
            identity=identity,
            parent_run_id=effective_parent_run_id,
        )
        await observation.resume_checkpointed(
            marker_id=checkpoint.marker_id,
            native_interrupt_ids=checkpoint.native_interrupt_ids,
        )
        if on_resume_checkpointed is not None:
            await on_resume_checkpointed(checkpoint)

    async def source(
        native_astream: Callable[..., AsyncIterator[Mapping[str, object]]],
    ) -> AsyncIterator[Mapping[str, object]]:
        raw_config = bound.arguments.get("config")
        if not isinstance(raw_config, Mapping):
            raise TypeError("bound Graph config must be a mapping")
        resolution = await bind_agui_lineage(
            native_astream,
            cast(RunnableConfig, raw_config),
            identity=identity,
            parent_run_id=parent_run_id,
            runtime_profile=tinkerfin.runtime_profile,
            resume=resume,
        )
        lineage_update = lineage_state_update(
            identity=identity,
            parent_run_id=resolution.parent_run_id,
            runtime_profile=tinkerfin.runtime_profile.profile_id,
            role=resolution.checkpoint_role,
        )
        if resume is not None:
            if resolution.resume_phase == "unstaged":
                resume_guard.mark_stage_started()
                resolution = await stage_agui_resume_intent(
                    native_astream,
                    resolution,
                    identity=identity,
                    runtime_profile=tinkerfin.runtime_profile,
                    resume=resume,
                    state_update=resume._state_update(
                        identity=identity,
                        parent_run_id=resolution.parent_run_id,
                        state_update=lineage_update,
                    ),
                )
            if resolution.resume_phase not in {"prepared", "accepted"}:
                raise TinkerFinLifecycleError(
                    "resume did not resolve to a durable intent"
                )
            # Both phases are proven saver-readable by bind/stage. From this point a
            # retry must preserve the claim and re-deliver the exact checkpoint callback.
            resume_guard.mark_marker_readable()
            bound.arguments["config"] = resolution.config
            bound.arguments["input"] = (
                resume._invocation_command()
                if resolution.resume_phase == "prepared"
                else None
            )
            bound.arguments["durability"] = "sync"
            await record_resume_checkpoint(resolution.parent_run_id)
        else:
            bound.arguments["config"] = resolution.config
            raw_input = bound.arguments.get("input")
            if not isinstance(raw_input, Mapping):
                raise TypeError("ordinary AG-UI Graph input must be a mapping")
            bound.arguments["input"] = {
                **cast(Mapping[str, object], raw_input),
                **lineage_update,
            }
        native = native_astream(*bound.args, **bound.kwargs)
        try:
            async for part in native:
                yield part
        finally:
            close = getattr(native, "aclose", None)
            if callable(close):
                # The Graph iterator is owned by this request source. Settle its close
                # independently so cancellation of the consumer cannot leak it.
                async def close_native() -> None:
                    await cast(Callable[[], Awaitable[object]], close)()

                close_task = asyncio.create_task(
                    close_native(),
                    name="tinkerfin-deep-agent-native-close",
                )
                await join_task(close_task)

    def source_factory() -> AsyncIterator[Mapping[str, object]]:
        nonlocal resolved_native_astream
        if resolved_native_astream is not None:
            raise TinkerFinLifecycleError("resume Graph source was already constructed")
        resolved_native_astream = native_astream_factory()
        return source(resolved_native_astream)

    stream = tinkerfin._run_agui(
        source_factory,
        identity=identity,
        parent_run_id=parent_run_id,
        on_part=on_part,
        timeout=timeout,
        settlement_timeout=settlement_timeout,
        expose_reasoning_events=expose_reasoning_events,
        expose_subagent_events=expose_subagent_events,
        prior_tool_call_ids=(
            frozenset() if resume is None else frozenset(resume.prior_tool_call_ids)
        ),
        private_state_keys=private_state_keys,
        on_event=on_event,
        observation=observation,
        on_settle=(resume_guard.settle_unprepared if resume is not None else None),
    )
    claim.claim()
    return stream


def _wrap_agui_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin,
    identity: RunIdentity,
    parent_run_id: str | None,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    on_event: EventObserver | None,
) -> AstreamT:
    """Preserve the Graph input signature for an ordinary AG-UI run."""

    signature = inspect.signature(astream)
    claim = _StreamClaim()
    native_astream = cast(
        Callable[..., AsyncIterator[Mapping[str, object]]],
        astream,
    )

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        graph_input = args[0] if args else kwargs.get("input")
        if graph_input is None or isinstance(graph_input, Command):
            raise ValueError(
                "ordinary AG-UI runs require Graph state input; use resume= for resume"
            )
        bound = tinkerfin._bind_native_invocation(
            signature,
            args,
            kwargs,
            identity=identity,
        )
        return _create_graph_agui_stream(
            native_astream_factory=lambda: native_astream,
            bound=bound,
            claim=claim,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            resume=None,
            on_resume_checkpointed=None,
            on_resume_initialization_failed=None,
            on_event=on_event,
        )

    return cast(AstreamT, wrapped)


def _resume_astream_signature(signature: inspect.Signature) -> inspect.Signature:
    """Remove Graph input and make config keyword-only for a resume Runtime."""

    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        parameters.pop(0)
    if not parameters or parameters[0].name != "input":
        raise TypeError("Graph astream must expose its input parameter")
    parameters.pop(0)
    parameters = [
        (
            parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            if parameter.name == "config"
            else parameter
        )
        for parameter in parameters
    ]
    return signature.replace(parameters=parameters)


def _flatten_bound_options(
    bound: inspect.BoundArguments,
    signature: inspect.Signature,
) -> dict[str, object]:
    """Restore keyword options after validating the public resume signature."""

    options: dict[str, object] = {}
    for name, value in bound.arguments.items():
        parameter = signature.parameters[name]
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            options.update(cast(Mapping[str, object], value))
        else:
            options[name] = value
    return options


def _wrap_agui_resume_astream(
    astream_factory: Callable[[], Callable[..., object]] | None,
    *,
    tinkerfin: TinkerFin,
    identity: RunIdentity,
    parent_run_id: str | None,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    resume: AgUiResumeBinding,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_resume_initialization_failed: (AgUiResumeInitializationFailureObserver | None),
    on_event: EventObserver | None,
) -> Callable[..., AgUiEventStream]:
    """Bind native resume input internally and expose only Graph options."""

    native_signature = tinkerfin.runtime_profile.astream_signature
    public_signature = _resume_astream_signature(native_signature)
    claim = _StreamClaim()

    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        public_bound = public_signature.bind(*args, **kwargs)
        options = _flatten_bound_options(public_bound, public_signature)
        bound = tinkerfin._bind_native_invocation(
            native_signature,
            (None,),
            options,
            identity=identity,
        )
        if resume.mode == "abandon":
            claim.ensure_available()

            async def empty_source() -> AsyncIterator[Mapping[str, object]]:
                if False:  # pragma: no cover - supplies the async iterator shape
                    yield {}

            context = source_context(
                identity=identity,
                runtime_profile=tinkerfin.runtime_profile.profile_id,
                input_kind="abandon",
                parent_run_id=parent_run_id,
                mode=mode,
                graph_input=resume.model_dump(mode="json", by_alias=True),
                config=bound.arguments.get("config", {}),
                private_state_keys=private_state_keys,
                resume=resume._observation_summaries(),
            )
            observation = tinkerfin._observation_hub(context)

            stream = tinkerfin._run_agui(
                empty_source,
                identity=identity,
                parent_run_id=parent_run_id,
                on_part=on_part,
                timeout=timeout,
                settlement_timeout=settlement_timeout,
                expose_reasoning_events=expose_reasoning_events,
                expose_subagent_events=expose_subagent_events,
                prior_tool_call_ids=frozenset(),
                private_state_keys=private_state_keys,
                on_event=on_event,
                observation=observation,
            )
            stream._resume_abandoned = True
            claim.claim()
            return stream
        if astream_factory is None:  # pragma: no cover - abandonment is handled above
            raise RuntimeError("resume Runtime has no Graph stream")
        resolved_astream_factory = astream_factory
        return _create_graph_agui_stream(
            native_astream_factory=lambda: cast(
                Callable[..., AsyncIterator[Mapping[str, object]]],
                resolved_astream_factory(),
            ),
            bound=bound,
            claim=claim,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_initialization_failed=on_resume_initialization_failed,
            on_event=on_event,
        )

    setattr(wrapped, "__signature__", public_signature)
    return wrapped


class DeepAgentRuntime(Generic[AstreamT]):
    """Single-request Runtime preserving the native LangGraph object stream."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: RunIdentity,
        mode: AgentMode,
        private_state_keys: frozenset[str],
        on_part: PartObserver[Mapping[str, object]] | None,
    ) -> None:
        """Bind a fresh native Graph stream to one canonical request.

        Args:
            astream: Fresh Graph stream callable owned by this request wrapper.
            tinkerfin: Immutable framework configuration borrowed by the Runtime.
            identity: Canonical thread and Run identity.
            mode: Resolved default or Plan routing mode.
            private_state_keys: Runtime-owned channels omitted from public facts.
            on_part: Optional callback invoked after validation and Observation.
        """

        self.astream = _wrap_native_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            mode=mode,
            private_state_keys=private_state_keys,
            on_part=on_part,
        )


class DeepAgentAgUiRuntime(Generic[AstreamT]):
    """Single-request AG-UI Runtime requiring one explicit Graph input."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: RunIdentity,
        parent_run_id: str | None,
        mode: AgentMode,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        private_state_keys: frozenset[str],
        on_event: EventObserver | None,
    ) -> None:
        """Bind a fresh Graph to one canonical ordinary AG-UI request.

        Args:
            astream: Fresh Graph stream callable used only by this request.
            tinkerfin: Immutable framework configuration borrowed by the Runtime.
            identity: Canonical thread and Run identity.
            parent_run_id: Optional branch source in the same thread.
            mode: Resolved default or Plan routing mode.
            on_part: Optional callback after Native validation and Observation.
            timeout: Optional total Native pull deadline in seconds.
            settlement_timeout: Optional caller wait for protected cleanup.
            expose_reasoning_events: Whether verified public reasoning is emitted.
            expose_subagent_events: Whether validated subagent events are emitted.
            private_state_keys: Runtime-owned state channels omitted from output.
            on_event: Optional callback awaited before public event delivery.
        """

        self.astream = _wrap_agui_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            on_event=on_event,
        )


class DeepAgentAgUiResumeRuntime(Generic[AstreamT]):
    """Single-request AG-UI Runtime whose native resume input is framework-owned."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream_factory: Callable[[], AstreamT] | None,
        tinkerfin: TinkerFin,
        identity: RunIdentity,
        parent_run_id: str | None,
        mode: AgentMode,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        private_state_keys: frozenset[str],
        resume: AgUiResumeBinding,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
        on_resume_initialization_failed: (
            AgUiResumeInitializationFailureObserver | None
        ),
        on_event: EventObserver | None,
    ) -> None:
        """Bind one validated resume without exposing its native Command.

        Args:
            astream_factory: Lazy fresh Graph callable factory, or ``None`` for full
                abandonment.
            tinkerfin: Immutable framework configuration borrowed by the Runtime.
            identity: New resume Run identity in the checkpoint thread.
            parent_run_id: Interrupted Run selected as the resume source.
            mode: Resolved default or Plan routing mode.
            on_part: Optional callback after Native validation and Observation.
            timeout: Optional total Native pull deadline in seconds.
            settlement_timeout: Optional caller wait for protected cleanup.
            expose_reasoning_events: Whether verified public reasoning is emitted.
            expose_subagent_events: Whether validated subagent events are emitted.
            private_state_keys: Runtime-owned state channels omitted from output.
            resume: Framework-resolved immutable resume facts.
            on_resume_checkpointed: Idempotent callback after marker durability.
            on_resume_initialization_failed: Idempotent host settlement invoked only
                when the stream closes before a resume marker is saver-readable.
            on_event: Optional callback awaited before public event delivery.
        """

        self.astream = _wrap_agui_resume_astream(
            astream_factory,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_initialization_failed=on_resume_initialization_failed,
            on_event=on_event,
        )


class DeepAgentDefinition(Generic[GraphT, AstreamT]):
    """Store one graph build call and create a fresh Graph for every Runtime."""

    __slots__ = (
        "_args",
        "_factory",
        "_get_astream",
        "_kwargs",
        "_plan_factory",
        "_plan_options",
        "_private_state_keys",
        "_tinkerfin",
        "_uncontracted_external_subagents",
    )

    def __init__(
        self,
        *,
        tinkerfin: TinkerFin,
        factory: Callable[..., GraphT],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        get_astream: Callable[[GraphT], AstreamT],
        plan_factory: Callable[..., object] | None,
        plan_options: PlanOptions | None,
        private_state_keys: frozenset[str],
        uncontracted_external_subagents: frozenset[str],
    ) -> None:
        """Freeze graph construction inputs while borrowing shared resources.

        The Definition opens no graph or external resource. Each ``new`` or
        ``new_agui`` call builds a fresh graph while reusing caller-owned model,
        checkpointer, Store, backend, and coordinator objects captured here.

        Args:
            tinkerfin: Immutable Runtime/Profile configuration.
            factory: Profile-selected Deep Agents graph factory.
            args: Positional graph-construction inputs retained by value.
            kwargs: Keyword graph-construction inputs retained by value.
            get_astream: Adapter from one fresh Graph to its stream callable.
            plan_factory: Optional fresh Planning Graph factory.
            plan_options: Optional immutable Plan configuration.
            private_state_keys: Runtime-owned channels excluded from public output.
            uncontracted_external_subagents: External graph names that cannot execute
                mixed Tool cancellation safely.
        """

        self._tinkerfin = tinkerfin
        self._factory = factory
        self._args = args
        self._kwargs = kwargs
        self._get_astream = get_astream
        self._plan_factory = plan_factory
        self._plan_options = plan_options
        self._private_state_keys = private_state_keys
        self._uncontracted_external_subagents = uncontracted_external_subagents

    def _build_astream(self, mode: AgentMode) -> AstreamT:
        """Build the native Graph and add only request-local Plan routing."""

        native = self._factory(*self._args, **self._kwargs)
        native_astream = self._get_astream(native)
        plan_factory = self._plan_factory
        if plan_factory is None:
            return native_astream
        plan_options = self._plan_options
        if plan_options is None:
            raise RuntimeError("Plan factory requires frozen Plan options")

        from .plan._runtime import PlanCapableGraphRuntime
        from .plan._workflow import PlanningWorkflowGraph

        runtime = PlanCapableGraphRuntime(
            options=plan_options,
            native=cast(_PlanNativeGraph, native),
            planning_factory=lambda: cast(
                PlanningWorkflowGraph[Any],
                plan_factory(*self._args, **self._kwargs),
            ),
            prefer_plan=mode == "plan",
        )
        return cast(AstreamT, runtime.astream)

    def new(
        self,
        *,
        identity: RunIdentity,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
    ) -> DeepAgentRuntime[AstreamT]:
        """Create a fresh Graph bound to one native object-stream request.

        Args:
            identity: Canonical identity shared by Runtime and Graph configuration.
            mode: Optional request-local default or Plan route.
            on_part: Optional callback after validation and Trace Observation, before
                the original part reaches the caller.

        Returns:
            A single-use Runtime whose stream yields original upstream objects.

        Raises:
            TypeError: Identity or callback has the wrong public type.
            ValueError: The selected mode is not available on this Definition.
        """

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        return DeepAgentRuntime(
            astream=self._build_astream(resolved_mode),
            tinkerfin=self._tinkerfin,
            identity=identity,
            mode=resolved_mode,
            private_state_keys=self._private_state_keys,
            on_part=on_part,
        )

    async def prepare_agui_resume(
        self,
        *,
        identity: RunIdentity,
        request: AgUiResumeRequest,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
    ) -> AgUiResumeBinding:
        """Resolve client resume entries from the canonical Graph checkpoint.

        Args:
            identity: New resume Run identity in the existing thread.
            request: Client decisions without server interrupt payloads.
            parent_run_id: Optional interrupted Run selected as the source.
            mode: Request-local default or Planning route used to inspect state.

        Returns:
            Private immutable binding accepted by :meth:`new_agui`.

        Raises:
            TypeError: Identity or request has the wrong public type.
            TinkerFinLifecycleError: Profile, lineage, checkpoint, interrupt, or Tool
                correlation evidence is missing, ambiguous, or inconsistent.
            AgUiResumeBindingError: Client coverage or decision validation fails.
        """

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from ._agui_lineage import resolve_agui_resume_context
        from .agui_resume import (
            AgUiResumeBinding as AgUiResumeBindingType,
        )
        from .agui_resume import (
            AgUiResumeRequest as AgUiResumeRequestType,
        )

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not isinstance(request, AgUiResumeRequestType):
            raise TypeError("request must be an AgUiResumeRequest")
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        astream = self._build_astream(resolved_mode)
        context = await resolve_agui_resume_context(
            cast(Callable[..., object], astream),
            identity=identity,
            parent_run_id=parent_run_id,
            runtime_profile=self._tinkerfin.runtime_profile,
        )
        return AgUiResumeBindingType.from_native(
            request=request,
            interrupts=context.interrupts,
            messages_by_namespace=context.messages_by_namespace,
        )

    def new_agui(
        self,
        *,
        identity: RunIdentity,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: AgUiResumeBinding | None = None,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None = None,
        on_resume_initialization_failed: (
            AgUiResumeInitializationFailureObserver | None
        ) = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[AstreamT] | DeepAgentAgUiResumeRuntime[AstreamT]:
        """Create one canonical ordinary or resume AG-UI Runtime.

        Args:
            identity: Canonical identity shared by lifecycle, Graph, checkpoint, and
                durable delivery.
            parent_run_id: Optional branch or resume source within the same thread.
            mode: Optional request-local default or Plan route.
            on_part: Optional observer for validated native stream parts.
            timeout: Optional total native-part pull deadline in seconds.
            settlement_timeout: Optional close-settlement wait per caller.
            expose_reasoning_events: Whether verified public reasoning emits events.
            expose_subagent_events: Whether validated subgraph events are emitted.
            resume: Optional binding resolved from a checkpoint or a trusted advanced
                AG-UI resume source.
            on_resume_checkpointed: Optional idempotent callback invoked after the exact
                resume marker is readable and before continuation output.
            on_resume_initialization_failed: Optional idempotent host settlement invoked
                after a pre-marker failure, cancellation, or close. It is never invoked
                after a prepared or accepted marker becomes saver-readable.
            on_event: Optional observer awaited before each public event is delivered.

        Returns:
            An ordinary Runtime requiring Graph input, or a resume Runtime whose input
            is already owned by the framework.

        Raises:
            TypeError: An identity, parent, binding, or callback has the wrong type.
            ValueError: Parent lineage or callback/binding combination is invalid.
            TinkerFinLifecycleError: Mixed cancellation reaches an unsupported external
                subagent contract.
        """

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from .agui_resume import AgUiResumeBinding as AgUiResumeBindingType

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        if resume is not None and not isinstance(resume, AgUiResumeBindingType):
            raise TypeError("resume must be an AgUiResumeBinding or None")
        if on_resume_checkpointed is not None and not callable(on_resume_checkpointed):
            raise TypeError("on_resume_checkpointed must be an async callable or None")
        if on_resume_initialization_failed is not None and not callable(
            on_resume_initialization_failed
        ):
            raise TypeError(
                "on_resume_initialization_failed must be an async callable or None"
            )
        if (
            resume is not None
            and resume.mode == "resume"
            and resume.contains_cancellations
        ):
            unsupported = (
                set(resume.source_agent_names) & self._uncontracted_external_subagents
            )
            if resume.unidentified_external_source or unsupported:
                raise TinkerFinLifecycleError(
                    "mixed Tool cancellation reached an external subagent without "
                    "the TinkerFin HITL contract",
                    context={"unsupported_subagents": ",".join(sorted(unsupported))},
                )
        if resume is None and on_resume_checkpointed is not None:
            raise ValueError("on_resume_checkpointed requires an AgUiResumeBinding")
        if resume is None and on_resume_initialization_failed is not None:
            raise ValueError(
                "on_resume_initialization_failed requires an AgUiResumeBinding"
            )
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        if resume is not None:
            return DeepAgentAgUiResumeRuntime(
                astream_factory=(
                    None
                    if resume.mode == "abandon"
                    else lambda: self._build_astream(resolved_mode)
                ),
                tinkerfin=self._tinkerfin,
                identity=identity,
                parent_run_id=parent_run_id,
                mode=resolved_mode,
                on_part=on_part,
                timeout=timeout,
                settlement_timeout=settlement_timeout,
                expose_reasoning_events=expose_reasoning_events,
                expose_subagent_events=expose_subagent_events,
                private_state_keys=self._private_state_keys,
                resume=resume,
                on_resume_checkpointed=on_resume_checkpointed,
                on_resume_initialization_failed=on_resume_initialization_failed,
                on_event=on_event,
            )
        return DeepAgentAgUiRuntime(
            astream=self._build_astream(resolved_mode),
            tinkerfin=self._tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            mode=resolved_mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=self._private_state_keys,
            on_event=on_event,
        )


class _EnhancedDeepAgentFactory(Generic[CreateP, GraphT, AstreamT]):
    """Preserve the public factory ParamSpec while resolving each Profile at runtime."""

    __slots__ = ("_factory", "_get_astream")

    def __init__(
        self,
        factory: Callable[CreateP, GraphT],
        get_astream: Callable[[GraphT], AstreamT],
    ) -> None:
        self._factory = factory
        self._get_astream = get_astream

    @overload
    def __get__(
        self,
        instance: None,
        owner: type[TinkerFin],
    ) -> _EnhancedDeepAgentFactory[CreateP, GraphT, AstreamT]: ...

    @overload
    def __get__(
        self,
        instance: TinkerFin,
        owner: type[TinkerFin],
    ) -> Callable[
        CreateP,
        DeepAgentDefinition[GraphT, AstreamT],
    ]: ...

    def __get__(
        self,
        instance: TinkerFin | None,
        owner: type[TinkerFin],
    ) -> object:
        if instance is None:
            return self

        # A Profile may adapt a newer upstream factory behind the current TinkerFin
        # build contract. Bind against that exact callable so defaults, HITL inputs,
        # state composition, and Plan forwarding cannot silently use the v2 template.
        selected_factory = cast(
            Callable[..., GraphT],
            instance._runtime_profile.create_agent_factory,
        )
        selected_signature = instance._runtime_profile.create_agent_signature

        @wraps(selected_factory)
        def create(
            *args: CreateP.args,
            **kwargs: CreateP.kwargs,
        ) -> DeepAgentDefinition[GraphT, AstreamT]:
            bound = selected_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            definition_kwargs = dict(kwargs)
            preparation = instance._runtime_profile.prepare_create_agent(
                cast(Mapping[str, object], bound.arguments)
            )
            definition_kwargs.update(preparation.keyword_overrides)
            definition_state = cast(
                type[DeepAgentState] | None,
                bound.arguments.get("state_schema"),
            )
            composed_state = compose_deep_agent_base_schema(
                instance._state_schema,
                definition_state,
            )
            if composed_state is not None:
                definition_kwargs["state_schema"] = composed_state
            plan_factory: Callable[..., object] | None = None
            private_state_keys = frozenset({LINEAGE_STATE_KEY, RESUME_MARKER_STATE_KEY})
            if instance._plan_options is not None:
                from .plan._state import PLAN_PRIVATE_STATE_KEYS
                from .plan._workflow import prepare_plan_factory

                plan_factory = prepare_plan_factory(
                    selected_signature,
                    instance._plan_options,
                )
                private_state_keys |= PLAN_PRIVATE_STATE_KEYS
            return DeepAgentDefinition(
                tinkerfin=instance,
                factory=selected_factory,
                args=cast(tuple[object, ...], args),
                kwargs=definition_kwargs,
                get_astream=self._get_astream,
                plan_factory=plan_factory,
                plan_options=instance._plan_options,
                private_state_keys=private_state_keys,
                uncontracted_external_subagents=(
                    preparation.uncontracted_external_subagents
                ),
            )

        # ``wraps`` follows the concrete callable, which process instrumentation may
        # replace with a broad ``*args, **kwargs`` wrapper. Preserve the Profile's
        # declared contract for IDEs, introspection, and deterministic preflight.
        setattr(create, "__signature__", selected_signature)
        return create


CREATE_DEEP_AGENT = _EnhancedDeepAgentFactory(
    _public_create_agent_contract,
    _graph_astream,
)


__all__ = [
    "DeepAgentAgUiRuntime",
    "DeepAgentDefinition",
    "DeepAgentRuntime",
]
