"""Deep Agents graph definitions and request-scoped runtimes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from functools import partial, wraps
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

from anyio import to_thread
from deepagents import graph as _deepagents_graph
from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import AgentMiddleware, InputAgentState
from langchain_core.callbacks import BaseCallbackManager
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.types import Command, StateSnapshot

from tinkerfin_contracts import RunIdentity
from tinkerfin_native_stream import NativeValidatedStreamPart

from ._agui_lineage_state import (
    LINEAGE_STATE_KEY,
    RESUME_MARKER_STATE_KEY,
    lineage_state_update,
)
from ._definition_trace import (
    DefinitionTraceContext,
    describe_middleware,
    skill_sources,
)
from ._observation import RuntimeObservationHub, source_context
from ._optional_dependencies import require_agui
from ._state_schema import compose_deep_agent_base_schema
from ._tasks import join_task
from .errors import TinkerFinLifecycleError
from .native_driver import NativeStreamDriver
from .plan._config import (
    AgentMode,
    PlanOptions,
    resolve_agent_mode,
)
from .runtime_profile import DeepAgentsRuntimeProfile

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
_GraphInput = InputAgentState | Command[object] | None
_NativeAstream = Callable[..., AsyncIterator[Mapping[str, object]]]
_NativeAstreamFactory = Callable[[], _NativeAstream | Awaitable[_NativeAstream]]


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


def _install_call_handler(
    bound: inspect.BoundArguments,
    observation: RuntimeObservationHub,
) -> None:
    """Append the request callback while preserving caller callback ownership."""

    if not observation.enabled:
        return
    raw_config = bound.arguments.get("config")
    if raw_config is None:
        config: dict[str, object] = {}
    elif isinstance(raw_config, Mapping):
        config = dict(cast(Mapping[str, object], raw_config))
    else:
        raise TypeError("bound Graph config must be a mapping or None")
    callbacks = config.get("callbacks")
    if callbacks is None:
        configured_callbacks: object = [observation.call_handler]
    elif isinstance(callbacks, list):
        configured_callbacks = [
            *cast(list[object], callbacks),
            observation.call_handler,
        ]
    elif isinstance(callbacks, BaseCallbackManager):
        manager = callbacks.copy()
        manager.add_handler(observation.call_handler, inherit=True)
        configured_callbacks = manager
    else:
        raise TypeError("config callbacks must be a callback list or manager")
    config["callbacks"] = configured_callbacks
    bound.arguments["config"] = config


async def _create_profile_graph(
    profile: DeepAgentsRuntimeProfile,
    factory: Callable[..., object],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> object:
    """Create one Profile-owned Graph through its optional async capability.

    Existing structural Profiles own synchronous factories through the base contract.
    Running that exact factory in AnyIO's bounded worker preserves the integration while
    keeping managed and direct graph creation non-blocking. A Profile may instead expose
    ``create_agent_graph`` when its factory requires native asynchronous construction.

    Args:
        profile: Selected integration that owns the exact factory and stream contract.
        factory: Concrete factory captured when the Definition was created.
        args: Frozen positional factory arguments.
        kwargs: Frozen keyword factory arguments.

    Returns:
        A fresh Profile-owned Graph for direct or managed execution.

    Raises:
        TypeError: An advertised async creation capability is not callable or awaitable.
        BaseException: Profile construction fails or caller cancellation propagates.
    """

    create_graph = getattr(profile, "create_agent_graph", None)
    if create_graph is None:
        build = partial(factory, *args, **dict(kwargs))
        return await to_thread.run_sync(build)
    if not callable(create_graph):
        raise TypeError("runtime_profile.create_agent_graph must be callable")
    pending = create_graph(factory, args, kwargs)
    if not inspect.isawaitable(pending):
        raise TypeError("runtime_profile.create_agent_graph must return an awaitable")
    return await pending


async def _close_owned_iterator(
    source: object,
    *,
    primary: BaseException | None,
    task_name: str,
) -> None:
    """Close one request-owned iterator without hiding execution or cancellation."""

    close = getattr(source, "aclose", None)
    if not callable(close):
        return

    async def close_source() -> None:
        await cast(Callable[[], Awaitable[object]], close)()

    task = asyncio.create_task(close_source(), name=task_name)
    try:
        await join_task(task)
    except BaseException as close_error:
        current = asyncio.current_task()
        if isinstance(close_error, asyncio.CancelledError) and (
            current is not None and current.cancelling()
        ):
            if primary is not None and not isinstance(primary, GeneratorExit):
                close_error.add_note(
                    "Iterator processing also failed: "
                    f"{type(primary).__name__}: {primary}"
                )
            raise
        if isinstance(primary, GeneratorExit):
            # GeneratorExit is the close signal, not the execution failure that owns
            # cleanup evidence. Surface the close failure to the enclosing Runtime so
            # it can attach that evidence to its actual processing error.
            raise close_error.with_traceback(close_error.__traceback__)
        if primary is None:
            raise
        primary.add_note(
            "Owned iterator cleanup also failed: "
            f"{type(close_error).__name__}: {close_error}"
        )


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


class _AsyncAstreamResolver:
    """Resolve one asynchronous Graph stream without spawning an orphan task.

    The first caller owns construction in its current cancellation scope. Concurrent
    callers await the same protected Future, while process-control exceptions propagate
    directly through the initiating call instead of escaping from a child task.
    """

    __slots__ = ("_factory", "_future", "_owner")

    def __init__(self, factory: _NativeAstreamFactory) -> None:
        self._factory = factory
        self._future: asyncio.Future[_NativeAstream] | None = None
        self._owner: asyncio.Task[object] | None = None

    async def resolve(self) -> _NativeAstream:
        """Return the one callable or propagate its retained construction outcome."""

        future = self._future
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods execute in a Task
            raise TinkerFinLifecycleError("Graph construction requires an asyncio task")
        if future is not None:
            if future.done():
                return future.result()
            if self._owner is current:
                raise TinkerFinLifecycleError("recursive Graph construction is invalid")
            return await asyncio.shield(future)

        future = asyncio.get_running_loop().create_future()
        self._future = future
        self._owner = current
        try:
            value = self._factory()
            if inspect.isawaitable(value):
                value = await value
            if not callable(value):
                raise TypeError("Graph factory must resolve to a callable astream")
            resolved = cast(_NativeAstream, value)
            future.set_result(resolved)
            return resolved
        except BaseException as error:
            future.set_exception(error)
            # The initiating caller re-raises the same outcome. Mark the Future as
            # observed so a request with no concurrent waiter cannot emit a warning.
            try:
                future.exception()
            except asyncio.CancelledError:
                pass
            raise
        finally:
            self._owner = None


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
        if probe is not None and await probe():
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
    trace_context: DefinitionTraceContext,
    on_part: PartObserver[Mapping[str, object]] | None,
) -> AstreamT:
    """Bind one Definition stream to its Profile, identity, and Observer lifecycle.

    Invocation validation happens before the single-use claim and before coordinator or
    Observer side effects. The wrapper preserves the installed callable signature while
    returning a TinkerFin-owned stream that keeps raw objects public and canonical
    frames private to downstream integrations.
    """

    signature = inspect.signature(astream)
    deferred_resolver = getattr(astream, "_tinkerfin_resolve_astream", None)
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
            call_tracking_enabled=True,
            middleware=trace_context.middleware,
            skill_sources=trace_context.skill_sources,
        )
        observation = tinkerfin._observation_hub(context)
        _install_call_handler(bound, observation)

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
            source_preflight=(
                cast(Callable[[], Awaitable[object]], deferred_resolver)
                if callable(deferred_resolver)
                else None
            ),
        )

    return cast(AstreamT, wrapped)


def _create_graph_agui_stream(
    *,
    native_astream_factory: _NativeAstreamFactory,
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
    trace_context: DefinitionTraceContext,
    checkpointer: object | None,
    resume: AgUiResumeBinding | None,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_resume_initialization_failed: (AgUiResumeInitializationFailureObserver | None),
    on_event: EventObserver | None,
) -> AgUiEventStream:
    """Create one lazy AG-UI stream from an already bound Graph call."""

    require_agui()
    from ._agui_lineage import (
        agui_resume_marker_is_durable,
        bind_agui_lineage,
        stage_agui_resume_intent,
    )

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
        call_tracking_enabled=True,
        middleware=trace_context.middleware,
        skill_sources=trace_context.skill_sources,
        resume=() if resume is None else resume._observation_summaries(),
    )
    observation = tinkerfin._observation_hub(context)
    _install_call_handler(bound, observation)
    resolver = _AsyncAstreamResolver(native_astream_factory)
    resolve_native_astream = resolver.resolve

    async def resume_marker_is_readable() -> bool:
        if resume is None:
            return False
        if checkpointer is not None:
            return await agui_resume_marker_is_durable(
                checkpointer,
                identity=identity,
                runtime_profile=tinkerfin.runtime_profile,
            )
        native_astream = await resolve_native_astream()
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
        if not isinstance(native, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        primary: BaseException | None = None
        try:
            async for part in native:
                yield part
        except BaseException as error:
            primary = error
            raise
        finally:
            await _close_owned_iterator(
                native,
                primary=primary,
                task_name="tinkerfin-deep-agent-native-close",
            )

    source_created = False

    async def deferred_source() -> AsyncIterator[Mapping[str, object]]:
        native_astream = await resolve_native_astream()
        opened = source(native_astream)
        primary: BaseException | None = None
        try:
            async for part in opened:
                yield part
        except BaseException as error:
            primary = error
            raise
        finally:
            # ``async for`` does not close a nested iterator when this outer source is
            # closed. The request owns that iterator and must settle it before Runtime
            # cancellation or terminal delivery can complete.
            await _close_owned_iterator(
                opened,
                primary=primary,
                task_name="tinkerfin-deep-agent-source-close",
            )

    def source_factory() -> AsyncIterator[Mapping[str, object]]:
        nonlocal source_created
        if source_created:
            raise TinkerFinLifecycleError("resume Graph source was already constructed")
        source_created = True
        return deferred_source()

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
        source_preflight=resolve_native_astream,
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
    trace_context: DefinitionTraceContext,
    on_event: EventObserver | None,
) -> AstreamT:
    """Preserve the Graph input signature for an ordinary AG-UI run."""

    signature = inspect.signature(astream)
    claim = _StreamClaim()
    native_astream = cast(_NativeAstream, astream)
    deferred_resolver = getattr(astream, "_tinkerfin_resolve_astream", None)
    native_astream_factory = (
        cast(_NativeAstreamFactory, deferred_resolver)
        if callable(deferred_resolver)
        else lambda: native_astream
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
            native_astream_factory=native_astream_factory,
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
            trace_context=trace_context,
            checkpointer=None,
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
    astream_factory: _NativeAstreamFactory | None,
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
    trace_context: DefinitionTraceContext,
    checkpointer: object | None,
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
                call_tracking_enabled=True,
                middleware=trace_context.middleware,
                skill_sources=trace_context.skill_sources,
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
            native_astream_factory=resolved_astream_factory,
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
            trace_context=trace_context,
            checkpointer=checkpointer,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_initialization_failed=on_resume_initialization_failed,
            on_event=on_event,
        )

    setattr(wrapped, "__signature__", public_signature)
    return wrapped


class DeepAgentGraph(Runnable[_GraphInput, Mapping[str, object]]):
    """Expose one complete async Plan/native Graph as a reusable Runnable.

    This direct Graph owns no request identity, Runtime Observation, Trace, AG-UI,
    Messaging, or business lifecycle. The selected Runtime Profile still binds and
    validates its concrete upstream stream shape so Plan routing never embeds a hidden
    v2 parser in Core. Callers use native LangGraph ``Command`` values for direct resume;
    managed AG-UI resume remains an ``open_agui_run`` responsibility.
    """

    __slots__ = ("_astream", "_driver", "_signature")

    def __init__(
        self,
        *,
        astream: Callable[..., AsyncIterator[Mapping[str, object]]],
        driver: NativeStreamDriver,
    ) -> None:
        """Bind one fresh effective Graph to its immutable Profile Driver.

        Args:
            astream: Effective native or Plan-capable upstream stream callable.
            driver: Selected Profile Driver used only for direct invocation binding and
                part validation. It does not create managed Runtime observations here.

        Raises:
            TypeError: The stream or Driver does not implement the required boundary.
        """

        if not callable(astream):
            raise TypeError("astream must be callable")
        if not isinstance(driver, NativeStreamDriver):
            raise TypeError("driver must implement NativeStreamDriver")
        self._astream = astream
        self._driver = driver
        self._signature = inspect.signature(astream)

    def invoke(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Mapping[str, object]:
        """Reject synchronous execution for an async resource Graph.

        Args:
            input: Ordinary state, native resume command, or ``None``.
            config: Optional LangGraph configuration, unused by the rejected call.
            **kwargs: Additional upstream options, unused by the rejected call.

        Raises:
            NotImplementedError: Always. Use :meth:`ainvoke` so async checkpointers,
                Stores, tools, and cancellation remain native.
        """

        del input, config, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    def stream(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any | None,
    ) -> Iterator[Mapping[str, object]]:
        """Reject synchronous streaming before any Graph or worker is started."""

        del input, config, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    def batch(
        self,
        inputs: list[_GraphInput],
        config: RunnableConfig | list[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any | None,
    ) -> list[Mapping[str, object]]:
        """Reject synchronous batching before LangChain creates worker threads."""

        del inputs, config, return_exceptions, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    def batch_as_completed(
        self,
        inputs: Sequence[_GraphInput],
        config: RunnableConfig | Sequence[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any | None,
    ) -> Iterator[tuple[int, Mapping[str, object]]]:
        """Reject synchronous concurrent batching without allocating an executor."""

        del inputs, config, return_exceptions, kwargs
        raise NotImplementedError("DeepAgentGraph supports async execution only")

    async def ainvoke(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Mapping[str, object]:
        """Run to one final root state while preserving native interrupt objects.

        Args:
            input: Ordinary state, native resume command, or ``None``.
            config: Optional LangGraph configuration.
            **kwargs: Additional Profile-supported async Graph options.

        Returns:
            A defensive root state mapping. Interrupted output includes the current
            LangGraph ``__interrupt__`` list, matching ``CompiledStateGraph.ainvoke``.

        Raises:
            TinkerFinLifecycleError: No root values boundary is emitted.
            BaseException: Profile binding, Graph execution, validation, or cleanup fails.
        """

        options = dict(kwargs)
        if callable(getattr(self._driver, "bind_graph_invocation", None)):
            options["stream_mode"] = ("values",)
        state: dict[str, object] | None = None
        async for _part, canonical in self._iterate(input, config, options):
            if canonical.type != "values" or canonical.ns != ():
                continue
            data = canonical.data
            if not isinstance(data, Mapping):
                raise TinkerFinLifecycleError(
                    "direct Graph root values data must be a mapping"
                )
            state = dict(cast(Mapping[str, object], data))
            interrupts = getattr(canonical, "interrupts", ())
            if interrupts:
                if isinstance(interrupts, str | bytes) or not isinstance(
                    interrupts, Sequence
                ):
                    raise TinkerFinLifecycleError(
                        "direct Graph root interrupts must be a sequence"
                    )
                state["__interrupt__"] = list(cast(Sequence[object], interrupts))
            else:
                state.pop("__interrupt__", None)
        if state is None:
            raise TinkerFinLifecycleError(
                "direct Graph completed without a root values boundary"
            )
        return state

    async def astream(
        self,
        input: _GraphInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Mapping[str, object]]:
        """Stream validated Profile objects without managed Runtime side effects.

        Profile-required semantic modes remain available to the internal Plan router. If
        the caller explicitly requests a subset, those private routing modes are consumed
        but not yielded. Omitting ``stream_mode`` exposes the Profile's complete canonical
        object stream, matching the managed native Runtime.

        Args:
            input: Ordinary state, native resume command, or ``None``.
            config: Optional LangGraph configuration.
            **kwargs: Profile-supported Graph stream options.

        Yields:
            Original upstream object mappings after Profile validation.

        Raises:
            TypeError: Stream mode or upstream iterator shape is invalid.
            ValueError: A direct Graph option conflicts with the selected Profile.
            BaseException: Graph execution, validation, cancellation, or cleanup fails.
        """

        requested = self._requested_modes(kwargs.get("stream_mode"))
        async for part, canonical in self._iterate(input, config, kwargs):
            if requested is None or canonical.type in requested:
                yield part

    async def _iterate(
        self,
        input: _GraphInput,
        config: RunnableConfig | None,
        options: Mapping[str, object],
    ) -> AsyncIterator[tuple[Mapping[str, object], NativeValidatedStreamPart]]:
        """Yield each raw object with its single validated Profile representation."""

        direct_binder = getattr(self._driver, "bind_graph_invocation", None)
        if callable(direct_binder):
            bound = cast(
                Callable[
                    [inspect.Signature, tuple[object, ...], Mapping[str, object]],
                    inspect.BoundArguments,
                ],
                direct_binder,
            )(
                self._signature,
                (input, config),
                options,
            )
        else:
            # Existing custom Profiles remain valid. They receive exactly the public
            # arguments their own stream signature declares; only Drivers that opt in
            # may add private modes needed by a composed direct Graph.
            bound = self._signature.bind(input, config, **dict(options))
        source = self._astream(*bound.args, **bound.kwargs)
        if not isinstance(source, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        primary: BaseException | None = None
        try:
            async for part in source:
                canonical = self._driver.validate(part)
                if not isinstance(part, Mapping):
                    raise TypeError("direct Graph stream parts must be mappings")
                yield part, canonical
        except BaseException as error:
            primary = error
            raise
        finally:
            await _close_owned_iterator(
                source,
                primary=primary,
                task_name="tinkerfin-direct-graph-close",
            )

    @staticmethod
    def _requested_modes(value: object) -> frozenset[str] | None:
        """Return an explicit caller mode filter without validating Profile support."""

        if value is None:
            return None
        if isinstance(value, str):
            return frozenset({value})
        if isinstance(value, Sequence) and not isinstance(value, bytes):
            modes: tuple[object, ...] = tuple(cast(Sequence[object], value))
            if all(isinstance(mode, str) for mode in modes):
                return frozenset(cast(str, mode) for mode in modes)
        return None


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
        trace_context: DefinitionTraceContext,
        on_part: PartObserver[Mapping[str, object]] | None,
    ) -> None:
        """Bind a fresh native Graph stream to one canonical request.

        Args:
            astream: Fresh Graph stream callable owned by this request wrapper.
            tinkerfin: Immutable framework configuration borrowed by the Runtime.
            identity: Canonical thread and Run identity.
            mode: Resolved default or Plan routing mode.
            private_state_keys: Runtime-owned channels omitted from public facts.
            trace_context: Visible middleware and configured Skill sources.
            on_part: Optional callback invoked after validation and Observation.
        """

        self.astream = _wrap_native_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            mode=mode,
            private_state_keys=private_state_keys,
            trace_context=trace_context,
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
        trace_context: DefinitionTraceContext,
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
            trace_context: Visible middleware and configured Skill sources.
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
            trace_context=trace_context,
            on_event=on_event,
        )


class DeepAgentAgUiResumeRuntime(Generic[AstreamT]):
    """Single-request AG-UI Runtime whose native resume input is framework-owned."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream_factory: Callable[[], AstreamT | Awaitable[AstreamT]] | None,
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
        trace_context: DefinitionTraceContext,
        checkpointer: object | None,
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
            trace_context: Visible middleware and configured Skill sources.
            checkpointer: Borrowed saver used for Graph-independent durable marker probes.
            resume: Framework-resolved immutable resume facts.
            on_resume_checkpointed: Idempotent callback after marker durability.
            on_resume_initialization_failed: Idempotent host settlement invoked only
                when the stream closes before a resume marker is saver-readable.
            on_event: Optional callback awaited before public event delivery.
        """

        self.astream = _wrap_agui_resume_astream(
            (
                None
                if astream_factory is None
                else cast(_NativeAstreamFactory, astream_factory)
            ),
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
            trace_context=trace_context,
            checkpointer=checkpointer,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_initialization_failed=on_resume_initialization_failed,
            on_event=on_event,
        )


class DeepAgentDefinition(Generic[GraphT, AstreamT]):
    """Store one Graph build call and create a fresh Graph for every execution."""

    __slots__ = (
        "_args",
        "_checkpointer",
        "_factory",
        "_get_astream",
        "_kwargs",
        "_plan_factory",
        "_plan_options",
        "_private_state_keys",
        "_tinkerfin",
        "_trace_context",
        "_uncontracted_external_subagents",
    )

    def __init__(
        self,
        *,
        tinkerfin: TinkerFin,
        factory: Callable[..., GraphT],
        args: tuple[object, ...],
        checkpointer: object | None,
        kwargs: dict[str, object],
        get_astream: Callable[[GraphT], AstreamT],
        plan_factory: Callable[..., object] | None,
        plan_options: PlanOptions | None,
        private_state_keys: frozenset[str],
        trace_context: DefinitionTraceContext,
        uncontracted_external_subagents: frozenset[str],
    ) -> None:
        """Freeze graph construction inputs while borrowing shared resources.

        The Definition opens no Graph or external resource. Direct ``create_graph``
        calls build immediately through the Profile's asynchronous boundary, while
        ``new`` and ``new_agui`` defer the same build until their stream is consumed.
        Every path reuses the caller-owned model, checkpointer, Store, backend, and
        coordinator objects captured here.

        Args:
            tinkerfin: Immutable Runtime/Profile configuration.
            factory: Profile-selected Deep Agents graph factory.
            args: Positional graph-construction inputs retained by value.
            checkpointer: Effective borrowed saver captured by this Definition.
            kwargs: Keyword graph-construction inputs retained by value.
            get_astream: Adapter from one fresh Graph to its stream callable.
            plan_factory: Optional fresh Planning Graph factory.
            plan_options: Optional immutable Plan configuration.
            private_state_keys: Runtime-owned channels excluded from public output.
            trace_context: Visible middleware and configured Skill source metadata.
            uncontracted_external_subagents: External graph names that cannot execute
                mixed Tool cancellation safely.
        """

        self._tinkerfin = tinkerfin
        self._factory = factory
        self._args = args
        self._checkpointer = checkpointer
        self._kwargs = kwargs
        self._get_astream = get_astream
        self._plan_factory = plan_factory
        self._plan_options = plan_options
        self._private_state_keys = private_state_keys
        self._trace_context = trace_context
        self._uncontracted_external_subagents = uncontracted_external_subagents

    def _deferred_astream(self, mode: AgentMode) -> AstreamT:
        """Return one signature-preserving stream that builds its Graph asynchronously."""

        async def build_astream() -> _NativeAstream:
            graph = await self.create_graph(mode=mode)
            return graph._astream

        resolver = _AsyncAstreamResolver(build_astream)
        resolve_astream = resolver.resolve

        def deferred(
            *args: object, **kwargs: object
        ) -> AsyncIterator[Mapping[str, object]]:
            async def source() -> AsyncIterator[Mapping[str, object]]:
                native_astream = await resolve_astream()
                native = native_astream(*args, **kwargs)
                if not isinstance(native, AsyncIterator):
                    raise TypeError("source_factory must return an async iterator")
                primary: BaseException | None = None
                try:
                    async for part in native:
                        yield part
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    await _close_owned_iterator(
                        native,
                        primary=primary,
                        task_name="tinkerfin-deferred-graph-close",
                    )

            return source()

        setattr(
            deferred, "__signature__", self._tinkerfin.runtime_profile.astream_signature
        )
        setattr(deferred, "_tinkerfin_resolve_astream", resolve_astream)
        return cast(AstreamT, deferred)

    async def create_graph(
        self,
        *,
        mode: AgentMode | None = None,
    ) -> DeepAgentGraph:
        """Create one complete reusable async native or Plan-capable Graph.

        The selected Runtime Profile owns asynchronous construction of the native Graph.
        Plan capability adds one lazily built Planning Graph to the same reusable router;
        it never creates a second native Graph. The returned Runnable owns no borrowed
        model, checkpointer, Store, backend, or cache resource and never closes them.

        Args:
            mode: Default route for new input. Resume always follows durable checkpoint
                role evidence rather than this preference.

        Returns:
            A reusable async Graph containing the Definition's complete native and
            optional Plan behavior.

        Raises:
            TypeError: The Profile factory does not produce the declared Graph boundary.
            ValueError: The requested mode is unavailable on this Definition.
            BaseException: Profile or Planning Graph construction fails.
        """

        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        native_value = await _create_profile_graph(
            self._tinkerfin.runtime_profile,
            cast(Callable[..., object], self._factory),
            self._args,
            self._kwargs,
        )
        native = cast(GraphT, native_value)
        native_astream = self._get_astream(native)
        if not callable(native_astream):
            raise TypeError("Runtime Profile Graph must expose a callable astream")
        effective: Callable[..., AsyncIterator[Mapping[str, object]]]
        plan_factory = self._plan_factory
        if plan_factory is None:
            effective = cast(
                Callable[..., AsyncIterator[Mapping[str, object]]],
                native_astream,
            )
        else:
            plan_options = self._plan_options
            if plan_options is None:
                raise RuntimeError("Plan factory requires frozen Plan options")
            from .plan._runtime import PlanCapableGraphRuntime
            from .plan._workflow import PlanningWorkflowGraph

            async def planning_graph() -> PlanningWorkflowGraph[Any]:
                build = partial(plan_factory, *self._args, **self._kwargs)
                value = await to_thread.run_sync(build)
                return cast(PlanningWorkflowGraph[Any], value)

            runtime = PlanCapableGraphRuntime(
                options=plan_options,
                native=cast(_PlanNativeGraph, native),
                planning_factory=planning_graph,
                prefer_plan=resolved_mode == "plan",
            )
            effective = runtime.astream
        return DeepAgentGraph(
            astream=effective,
            driver=self._tinkerfin.runtime_profile.stream_driver,
        )

    def _belongs_to(self, family: object) -> bool:
        """Return whether this Definition came from one configured factory family."""

        return self._tinkerfin._family is family

    def _resume_checkpointer(self) -> object | None:
        """Return the effective borrowed saver without transferring ownership."""

        return self._checkpointer

    async def _open_native_run(
        self,
        *,
        identity: RunIdentity,
        input: _GraphInput,
        mode: AgentMode | None,
        config: RunnableConfig | None,
        context: object | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        stream_options: Mapping[str, object],
    ) -> NativeGraphRunStream:
        """Build once and return one managed native stream for the common facade."""

        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = await self.create_graph(mode=resolved_mode)
        runtime = DeepAgentRuntime(
            astream=cast(AstreamT, graph._astream),
            tinkerfin=self._tinkerfin,
            identity=identity,
            mode=resolved_mode,
            private_state_keys=self._private_state_keys,
            trace_context=self._trace_context,
            on_part=on_part,
        )
        return cast(
            "NativeGraphRunStream",
            runtime.astream(
                input,
                config,
                context=context,
                **dict(stream_options),
            ),
        )

    async def _open_agui_run(
        self,
        *,
        identity: RunIdentity,
        input: InputAgentState | None,
        resume_request: AgUiResumeRequest | None,
        parent_run_id: str | None,
        mode: AgentMode | None,
        config: RunnableConfig | None,
        context: object | None,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
        on_resume_initialization_failed: (
            AgUiResumeInitializationFailureObserver | None
        ),
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        on_part: PartObserver[Mapping[str, object]] | None,
        on_event: EventObserver | None,
        stream_options: Mapping[str, object],
    ) -> AgUiEventStream:
        """Build once and return one managed AG-UI stream for the common facade."""

        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = await self.create_graph(mode=resolved_mode)
        if resume_request is None:
            if input is None:
                raise ValueError("ordinary AG-UI run requires input")
            runtime = DeepAgentAgUiRuntime(
                astream=cast(AstreamT, graph._astream),
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
                trace_context=self._trace_context,
                on_event=on_event,
            )
            return cast(
                "AgUiEventStream",
                runtime.astream(
                    input,
                    config,
                    context=context,
                    **dict(stream_options),
                ),
            )

        binding = await self._prepare_agui_resume_from_astream(
            graph._astream,
            identity=identity,
            request=resume_request,
            parent_run_id=parent_run_id,
        )
        resume_runtime = DeepAgentAgUiResumeRuntime(
            astream_factory=(
                None
                if binding.mode == "abandon"
                else lambda: cast(AstreamT, graph._astream)
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
            trace_context=self._trace_context,
            checkpointer=self._checkpointer,
            resume=binding,
            on_resume_checkpointed=on_resume_checkpointed,
            on_resume_initialization_failed=on_resume_initialization_failed,
            on_event=on_event,
        )
        return resume_runtime.astream(
            config=config,
            context=context,
            **dict(stream_options),
        )

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
            astream=self._deferred_astream(resolved_mode),
            tinkerfin=self._tinkerfin,
            identity=identity,
            mode=resolved_mode,
            private_state_keys=self._private_state_keys,
            trace_context=self._trace_context,
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

        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = await self.create_graph(mode=resolved_mode)
        return await self._prepare_agui_resume_from_astream(
            graph._astream,
            identity=identity,
            request=request,
            parent_run_id=parent_run_id,
        )

    async def _prepare_agui_resume_from_astream(
        self,
        astream: Callable[..., object],
        *,
        identity: RunIdentity,
        request: AgUiResumeRequest,
        parent_run_id: str | None,
    ) -> AgUiResumeBinding:
        """Resolve one public resume request against an already built Graph."""

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from ._agui_lineage import resolve_agui_resume_context
        from .agui_resume import AgUiResumeBinding as AgUiResumeBindingType
        from .agui_resume import AgUiResumeRequest as AgUiResumeRequestType

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        if not isinstance(request, AgUiResumeRequestType):
            raise TypeError("request must be an AgUiResumeRequest")
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        context = await resolve_agui_resume_context(
            astream,
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
            deferred_astream = self._deferred_astream(resolved_mode)
            deferred_resolver = getattr(
                deferred_astream,
                "_tinkerfin_resolve_astream",
                None,
            )
            if not callable(deferred_resolver):  # pragma: no cover - internal invariant
                raise RuntimeError("deferred Graph stream has no async resolver")
            return DeepAgentAgUiResumeRuntime(
                astream_factory=(
                    None
                    if resume.mode == "abandon"
                    else cast(
                        Callable[[], AstreamT | Awaitable[AstreamT]],
                        deferred_resolver,
                    )
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
                trace_context=self._trace_context,
                checkpointer=self._checkpointer,
                resume=resume,
                on_resume_checkpointed=on_resume_checkpointed,
                on_resume_initialization_failed=on_resume_initialization_failed,
                on_event=on_event,
            )
        return DeepAgentAgUiRuntime(
            astream=self._deferred_astream(resolved_mode),
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
            trace_context=self._trace_context,
            on_event=on_event,
        )


class _EnhancedDeepAgentFactory(Generic[CreateP, GraphT, AstreamT]):
    """Preserve the public factory ParamSpec while resolving each Profile at runtime."""

    __slots__ = ("_factory",)

    def __init__(
        self,
        factory: Callable[CreateP, GraphT],
    ) -> None:
        self._factory = factory

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
            resolved_middleware, middleware_descriptors = describe_middleware(
                bound.arguments.get("middleware", ())
            )
            bound.arguments["middleware"] = resolved_middleware
            definition_kwargs["middleware"] = resolved_middleware
            trace_context = DefinitionTraceContext(
                middleware=middleware_descriptors,
                skill_sources=skill_sources(dict(bound.arguments)),
            )
            checkpointer = bound.arguments.get("checkpointer")
            if checkpointer is None and instance._checkpointer is not None:
                checkpointer = instance._checkpointer
                bound.arguments["checkpointer"] = checkpointer
                definition_kwargs["checkpointer"] = checkpointer
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
                from .plan._handoff import create_plan_handoff_middleware
                from .plan._state import PLAN_PRIVATE_STATE_KEYS
                from .plan._workflow import prepare_plan_factory

                middleware = cast(
                    Sequence[AgentMiddleware[Any, Any, Any]],
                    definition_kwargs.get("middleware", ()),
                )
                definition_kwargs["middleware"] = (
                    *middleware,
                    create_plan_handoff_middleware(),
                )
                plan_factory = prepare_plan_factory(
                    selected_signature,
                    instance._plan_options,
                )
                private_state_keys |= PLAN_PRIVATE_STATE_KEYS
            return DeepAgentDefinition(
                tinkerfin=instance,
                factory=selected_factory,
                args=cast(tuple[object, ...], args),
                checkpointer=checkpointer,
                kwargs=definition_kwargs,
                get_astream=lambda graph: cast(
                    AstreamT,
                    instance._runtime_profile.graph_stream(graph),
                ),
                plan_factory=plan_factory,
                plan_options=instance._plan_options,
                private_state_keys=private_state_keys,
                trace_context=trace_context,
                uncontracted_external_subagents=(
                    preparation.uncontracted_external_subagents
                ),
            )

        # ``wraps`` follows the concrete callable, which process instrumentation may
        # replace with a broad ``*args, **kwargs`` wrapper. Preserve the Profile's
        # declared contract for IDEs, introspection, and deterministic preflight.
        setattr(create, "__signature__", selected_signature)
        return create


CREATE_DEEP_AGENT: _EnhancedDeepAgentFactory[..., object, _NativeAstream] = (
    _EnhancedDeepAgentFactory(
        _public_create_agent_contract,
    )
)


__all__ = [
    "DeepAgentAgUiRuntime",
    "DeepAgentDefinition",
    "DeepAgentRuntime",
]
