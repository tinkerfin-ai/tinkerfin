"""Runtime Observation ordering, failure propagation, and terminal ownership."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from ag_ui.core import RunErrorEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AIMessageChunk
from langgraph.types import Interrupt, StreamMode

from tinkerfin import (
    AgUiResumeBinding,
    DeepAgentDefinition,
    DeepAgentsFactoryPreparation,
    DeepAgentsV2RuntimeProfile,
    DeepAgentsV2StreamDriver,
    NativeStreamFrame,
    RunIdentity,
    RunObservationError,
    TinkerFin,
    TinkerFinStreamProtocolError,
)
from tinkerfin_contracts import (
    NativeStateObservation,
    ObservationBoundary,
    RunObservationSession,
    RunResumeSummary,
    RunSourceContext,
    RuntimeObservation,
)
from tinkerfin_native_stream import NativeStreamPart, NativeValuesStreamPart


class _Session:
    def __init__(
        self,
        *,
        order: list[str] | None = None,
        fail_kind: str | None = None,
    ) -> None:
        self.observations: list[RuntimeObservation] = []
        self.boundaries: list[ObservationBoundary] = []
        self.closed = 0
        self.failure: asyncio.Future[BaseException] | None = None
        self.order = order
        self.fail_kind = fail_kind

    async def observe(self, observation: RuntimeObservation) -> None:
        self.observations.append(observation)
        if self.order is not None:
            self.order.append(f"trace:{observation.kind}")
        if observation.kind == self.fail_kind:
            raise RuntimeError("observer failed")

    async def force(self, boundary: ObservationBoundary) -> None:
        self.boundaries.append(boundary)

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        if self.failure is None:
            self.failure = asyncio.get_running_loop().create_future()
        return self.failure

    async def aclose(self) -> None:
        self.closed += 1


class _Observer:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.contexts: list[RunSourceContext] = []

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        self.contexts.append(context)
        return self.session


class _ControlSession(_Session):
    def __init__(self, *, phase: str, error: BaseException) -> None:
        super().__init__()
        self.phase = phase
        self.error = error

    async def observe(self, observation: RuntimeObservation) -> None:
        if self.phase == "observe" and observation.kind == "run.started":
            raise self.error
        await super().observe(observation)

    async def force(self, boundary: ObservationBoundary) -> None:
        if self.phase == "force":
            raise self.error
        await super().force(boundary)

    async def aclose(self) -> None:
        self.closed += 1
        if self.phase == "close":
            raise self.error


class _OpeningControlObserver:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        del context
        raise self.error


class _BlockingClosedSession(_Session):
    def __init__(self) -> None:
        super().__init__()
        self.closed_observation_started = asyncio.Event()
        self._never = asyncio.Event()

    async def observe(self, observation: RuntimeObservation) -> None:
        await super().observe(observation)
        if observation.kind == "run.closed":
            self.closed_observation_started.set()
            await self._never.wait()


class _Graph:
    def __init__(
        self,
        parts: list[object],
        *,
        pull_gate: asyncio.Event | None = None,
    ) -> None:
        self.parts = parts
        self.pull_gate = pull_gate
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def astream(
        self,
        input: object,
        config: object | None = None,
        *,
        context: object | None = None,
        stream_mode: StreamMode | tuple[StreamMode, ...] | None = None,
        print_mode: StreamMode | tuple[StreamMode, ...] = (),
        output_keys: str | tuple[str, ...] | None = None,
        interrupt_before: str | tuple[str, ...] | None = None,
        interrupt_after: str | tuple[str, ...] | None = None,
        durability: str | None = None,
        control: object | None = None,
        subgraphs: bool = False,
        debug: bool | None = None,
        version: str = "v1",
        **kwargs: object,
    ) -> AsyncIterator[object]:
        del (
            input,
            config,
            context,
            stream_mode,
            print_mode,
            output_keys,
            interrupt_before,
            interrupt_after,
            durability,
            control,
            subgraphs,
            debug,
            version,
            kwargs,
        )
        self.started.set()
        try:
            if self.pull_gate is not None:
                await self.pull_gate.wait()
            for part in self.parts:
                yield part
        finally:
            self.closed.set()


def _identity() -> RunIdentity:
    return RunIdentity(threadId="thread-observed", runId="run-observed")


def _input() -> InputAgentState:
    return InputAgentState(messages=[])


def _source_context() -> RunSourceContext:
    return RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )


def _definition(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
    graph: object,
    observer: _Observer,
) -> DeepAgentDefinition[None]:
    return definition_factory(graph, tinkerfin=TinkerFin().observe(observer))


async def test_observe_is_immutable_and_plan_preserves_registration(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    base = TinkerFin()
    session = _Session()
    observer = _Observer(session)
    observed = base.observe(observer)
    planned = observed.plan(enabled=False)

    plain_stream = (
        definition_factory(_Graph([]), tinkerfin=base)
        .new(identity=_identity())
        .astream(_input())
    )
    observed_stream = (
        definition_factory(_Graph([]), tinkerfin=planned)
        .new(identity=_identity())
        .astream(_input())
    )

    assert [part async for part in plain_stream] == []
    assert [part async for part in observed_stream] == []
    assert len(observer.contexts) == 1
    with pytest.raises(ValueError, match="same RuntimeObserver"):
        observed.observe(observer)


async def test_native_observation_precedes_on_part_and_delivery(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    order: list[str] = []
    session = _Session(order=order)
    observer = _Observer(session)
    part = {
        "type": "values",
        "ns": (),
        "data": {"messages": [], "todos": []},
        "interrupts": (),
    }

    async def on_part(_part: object) -> None:
        order.append("hook")

    stream = (
        _definition(definition_factory, _Graph([part]), observer)
        .new(
            identity=_identity(),
            on_part=on_part,
        )
        .astream(_input())
    )
    delivered = [value async for value in stream]
    order.append("delivered")

    assert delivered == [part]
    assert order == [
        "trace:run.started",
        "trace:run.input",
        "trace:native.state",
        "hook",
        "trace:run.terminal",
        "trace:run.closed",
        "delivered",
    ]
    assert cast(object, session.observations[-2]).outcome == "succeeded"  # type: ignore[attr-defined]
    assert session.boundaries == [
        ObservationBoundary.TERMINAL,
        ObservationBoundary.CLOSE,
    ]
    assert session.closed == 1


async def test_malformed_part_never_reaches_trace_native_or_on_part(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    hook_called = False

    async def on_part(_part: object) -> None:
        nonlocal hook_called
        hook_called = True

    malformed = {
        "type": "messages",
        "ns": (),
        "data": (AIMessageChunk(id="message-1", content="partial"),),
    }
    stream = (
        _definition(definition_factory, _Graph([malformed]), observer)
        .new(
            identity=_identity(),
            on_part=on_part,
        )
        .astream(_input())
    )

    with pytest.raises(TinkerFinStreamProtocolError):
        await anext(stream)

    assert hook_called is False
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
        "run.terminal",
        "run.closed",
    ]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "failed"


async def test_root_interrupt_selects_interrupted_terminal(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    part = {
        "type": "values",
        "ns": (),
        "data": {"messages": []},
        "interrupts": (Interrupt(value={"request": "approval"}, id="interrupt-1"),),
    }
    stream = (
        _definition(definition_factory, _Graph([part]), observer)
        .new(identity=_identity())
        .astream(_input())
    )

    assert [value async for value in stream] == [part]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "interrupted"
    assert terminal.interrupt_ids == ("interrupt-1",)
    assert session.boundaries[-2:] == [
        ObservationBoundary.INTERRUPT,
        ObservationBoundary.CLOSE,
    ]


async def test_explicit_close_selects_cancelled_terminal(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    part = {"type": "values", "ns": (), "data": {"messages": []}}
    stream = (
        _definition(definition_factory, _Graph([part, part]), observer)
        .new(identity=_identity())
        .astream(_input())
    )

    assert await anext(stream) == part
    await stream.aclose()

    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "cancelled"


async def test_cancelled_resume_without_graph_selects_abandoned_terminal(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    binding = AgUiResumeBinding(
        mode="abandon",
        resume_data=None,
        native_interrupt_ids=("interrupt-1",),
    )
    runtime = _definition(definition_factory, _Graph([]), observer).new_agui(
        identity=_identity(),
        resume=binding,
    )

    events = [event async for event in runtime.astream()]

    assert events[-1].type.value == "RUN_ERROR"
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "abandoned"
    assert observer.contexts[0].resume == (
        RunResumeSummary(
            interrupt_id="interrupt-1",
            status="cancelled",
        ),
    )


async def test_agui_starts_observation_before_delivering_run_started(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    session = _Session()
    observer = _Observer(session)
    stream = (
        _definition(definition_factory, _Graph([]), observer)
        .new_agui(identity=_identity())
        .astream(_input())
    )

    started = await anext(stream)

    assert started.type.value == "RUN_STARTED"
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
    ]
    await stream.aclose()
    assert [item.kind for item in session.observations[-2:]] == [
        "run.terminal",
        "run.closed",
    ]
    assert session.observations[-2].outcome == "cancelled"  # type: ignore[attr-defined]


async def test_failing_observer_notifies_healthy_observer_then_fails_run(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    failed = _Session(fail_kind="native.state")
    healthy = _Session()
    failed_observer = _Observer(failed)
    healthy_observer = _Observer(healthy)
    tinkerfin = TinkerFin().observe(failed_observer).observe(healthy_observer)
    part = {"type": "values", "ns": (), "data": {"messages": []}}
    stream = (
        definition_factory(_Graph([part]), tinkerfin=tinkerfin)
        .new(identity=_identity())
        .astream(_input())
    )

    with pytest.raises(RunObservationError) as captured:
        await anext(stream)

    assert captured.value.__cause__ is not None
    assert [item.kind for item in healthy.observations] == [
        "run.started",
        "run.input",
        "native.state",
        "run.observer_failed",
        "run.terminal",
        "run.closed",
    ]
    assert healthy.observations[-2].outcome == "failed"  # type: ignore[attr-defined]
    assert failed.closed == healthy.closed == 1


async def test_terminal_observer_failure_does_not_rewrite_the_selected_outcome(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    failed = _Session(fail_kind="run.terminal")
    healthy = _Session()
    tinkerfin = TinkerFin().observe(_Observer(failed)).observe(_Observer(healthy))
    stream = (
        definition_factory(_Graph([]), tinkerfin=tinkerfin)
        .new(identity=_identity())
        .astream(_input())
    )

    with pytest.raises(RunObservationError):
        _ = [part async for part in stream]

    assert [item.kind for item in healthy.observations] == [
        "run.started",
        "run.input",
        "run.terminal",
        "run.observer_failed",
        "run.closed",
    ]
    terminals = [item for item in healthy.observations if item.kind == "run.terminal"]
    assert len(terminals) == 1
    assert terminals[0].outcome == "succeeded"  # type: ignore[attr-defined]
    assert healthy.observations[-1].outcome == "succeeded"  # type: ignore[attr-defined]
    assert failed.closed == healthy.closed == 1


async def test_background_observer_failure_cancels_blocked_graph_pull(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    gate = asyncio.Event()
    graph = _Graph([], pull_gate=gate)
    session = _Session()
    observer = _Observer(session)
    stream = (
        _definition(definition_factory, graph, observer)
        .new(identity=_identity())
        .astream(_input())
    )
    pull = asyncio.create_task(anext(stream))
    await graph.started.wait()
    assert session.failure is not None

    session.failure.set_result(RuntimeError("background writer failed"))
    with pytest.raises(RunObservationError):
        await pull

    assert graph.closed.is_set()
    assert session.closed == 1


async def test_observation_close_preserves_caller_cancellation_and_settles_sessions() -> (
    None
):
    session = _BlockingClosedSession()
    observer = _Observer(session)
    tinkerfin = TinkerFin().observe(observer)
    context = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    hub = tinkerfin._observation_hub(context)
    await hub.start()
    await hub.terminal("cancelled", code="cancelled")
    closing = asyncio.create_task(hub.close())
    await session.closed_observation_started.wait()

    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert closing.cancelled() is True
    assert session.closed == 1


@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt])
async def test_observer_open_preserves_process_control(
    error_type: type[BaseException],
) -> None:
    error = error_type("process-control")
    hub = (
        TinkerFin()
        .observe(_OpeningControlObserver(error))
        ._observation_hub(_source_context())
    )

    with pytest.raises(error_type) as captured:
        await hub.start()

    assert captured.value is error


@pytest.mark.parametrize("phase", ["observe", "force", "failure_waiter", "close"])
@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt])
async def test_observer_lifecycle_preserves_process_control(
    phase: str,
    error_type: type[BaseException],
) -> None:
    error = error_type("process-control")
    session = _ControlSession(phase=phase, error=error)
    hub = TinkerFin().observe(_Observer(session))._observation_hub(_source_context())

    if phase == "observe":
        with pytest.raises(error_type) as captured:
            await hub.start()
        assert captured.value is error
        await hub.close()
        return

    await hub.start()
    if phase == "failure_waiter":
        session.failure_waiter().set_result(error)
        operation = hub.wait_failure()
    elif phase == "force":
        operation = hub.force(ObservationBoundary.RESUME_CHECKPOINTED)
    else:
        operation = hub.close()

    with pytest.raises(error_type) as captured:
        await operation

    assert captured.value is error
    if phase != "close":
        await hub.close()


async def test_failed_agui_run_records_input_terminal_and_close() -> None:
    session = _Session()
    observer = _Observer(session)
    stream = (
        TinkerFin()
        .observe(observer)
        .failed_agui_run(
            RuntimeError("model setup failed"),
            identity=_identity(),
            input={"messages": []},
            config={"configurable": {"thread_id": "thread-observed"}},
        )
    )

    events = [event async for event in stream]

    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "runtime_initialization_error"
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
        "run.terminal",
        "run.closed",
    ]
    terminal = session.observations[-2]
    assert terminal.kind == "run.terminal"
    assert terminal.outcome == "failed"
    assert terminal.error_type == "builtins.RuntimeError"


async def test_failed_resume_resolution_preserves_resume_input_kind() -> None:
    from ag_ui.core.types import ResumeEntry

    from tinkerfin import AgUiResumeRequest

    session = _Session()
    observer = _Observer(session)
    request = AgUiResumeRequest(
        entries=(
            ResumeEntry(
                interrupt_id="interrupt-1#0",
                status="resolved",
                payload={"type": "approve"},
            ),
        )
    )
    stream = (
        TinkerFin()
        .observe(observer)
        .failed_agui_run(
            RuntimeError("checkpoint resolution failed"),
            identity=_identity(),
            config={"configurable": {"thread_id": "thread-observed"}},
            resume_request=request,
        )
    )

    events = [event async for event in stream]

    assert isinstance(events[-1], RunErrorEvent)
    run_input = session.observations[1]
    assert run_input.kind == "run.input"
    assert run_input.source.input_kind == "resume"
    assert run_input.source.resume == ()


async def test_agui_reuses_the_runtime_structural_validation_result(
    definition_factory: Callable[..., DeepAgentDefinition[None]],
) -> None:
    class CountingDriver(DeepAgentsV2StreamDriver):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def normalize(
            self,
            part: object,
            *,
            context: RunSourceContext,
        ) -> NativeStreamFrame:
            self.calls += 1
            return super().normalize(part, context=context)

    class CountingProfile(DeepAgentsV2RuntimeProfile):
        def __init__(self, driver: CountingDriver) -> None:
            super().__init__()
            self._driver = driver

        @property
        def stream_driver(self) -> CountingDriver:
            return self._driver

    driver = CountingDriver()
    part = {"type": "values", "ns": (), "data": {"messages": []}}
    runtime = definition_factory(
        _Graph([part]),
        tinkerfin=TinkerFin(runtime_profile=CountingProfile(driver)),
    ).new_agui(identity=_identity())

    events = [event async for event in runtime.astream(_input())]

    assert events[-1].type.value == "RUN_FINISHED"
    assert driver.calls == 1


class _FixtureGraph:
    def __init__(self) -> None:
        self.options: dict[str, object] | None = None

    async def astream(
        self,
        input: object,
        config: object | None = None,
        *,
        fixture_mode: str | None = None,
    ) -> AsyncIterator[object]:
        del input
        self.options = {"config": config, "fixture_mode": fixture_mode}
        yield {"fixture_payload": "value"}


class _FixtureStreamDriver:
    def __init__(self) -> None:
        self.normalize_calls = 0

    def bind_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
        runtime_profile: str,
    ) -> inspect.BoundArguments:
        bound = signature.bind(*args, **dict(options))
        bound.arguments["config"] = {
            "configurable": {
                "thread_id": identity.thread_id,
                "_tinkerfin_runtime_profile": runtime_profile,
            }
        }
        bound.arguments["fixture_mode"] = "canonical"
        return bound

    def validate(self, part: object) -> NativeValuesStreamPart:
        if part != {"fixture_payload": "value"}:
            raise ValueError("fixture source emitted an unexpected object")
        return NativeValuesStreamPart(
            type="values",
            ns=(),
            data={"messages": [], "fixture": "value"},
            interrupts=(),
        )

    def normalize(
        self,
        part: object,
        *,
        context: RunSourceContext,
    ) -> NativeStreamFrame:
        self.normalize_calls += 1
        canonical = self.validate(part)
        observation = NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"fixture": "value"},
            messages=(),
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=0,
        )
        return NativeStreamFrame(
            canonical=canonical,
            observations=(observation,),
            replay=NativeStreamPart(
                type="values",
                ns=(),
                data={"fixture": "value"},
            ),
        )


class _FixtureRuntimeProfile(DeepAgentsV2RuntimeProfile):
    def __init__(self, graph: _FixtureGraph) -> None:
        self._graph = graph
        self._driver = _FixtureStreamDriver()

    @property
    def profile_id(self) -> str:
        return "fixture-profile"

    @property
    def create_agent_factory(self) -> Callable[..., object]:
        return self._build

    @property
    def create_agent_signature(self) -> inspect.Signature:
        return inspect.signature(self._build)

    @property
    def astream_signature(self) -> inspect.Signature:
        return inspect.signature(self._graph.astream)

    def prepare_create_agent(
        self,
        arguments: Mapping[str, object],
    ) -> DeepAgentsFactoryPreparation:
        del arguments
        return DeepAgentsFactoryPreparation(keyword_overrides={})

    @property
    def stream_driver(self) -> _FixtureStreamDriver:
        return self._driver

    def _build(self, *_args: object, **_kwargs: object) -> _FixtureGraph:
        return self._graph


async def test_profile_maps_a_non_v2_source_once_for_observer_and_agui() -> None:
    graph = _FixtureGraph()
    profile = _FixtureRuntimeProfile(graph)
    session = _Session()
    definition = (
        TinkerFin(runtime_profile=profile)
        .observe(_Observer(session))
        .create_deep_agent(model="provider:model", tools=[])
    )
    runtime = definition.new_agui(identity=_identity())

    events = [event async for event in runtime.astream(_input())]

    assert graph.options is not None
    assert graph.options["fixture_mode"] == "canonical"
    raw_config = graph.options["config"]
    assert isinstance(raw_config, Mapping)
    configurable = raw_config["configurable"]
    assert isinstance(configurable, Mapping)
    assert configurable["thread_id"] == "thread-observed"
    assert configurable["run_id"] == "run-observed"
    assert configurable["_tinkerfin_runtime_profile"] == "fixture-profile"
    assert not ({"version", "stream_mode", "subgraphs"} & set(graph.options))
    assert profile.stream_driver.normalize_calls == 1
    assert any(event.type.value == "STATE_SNAPSHOT" for event in events)
    assert [item.kind for item in session.observations] == [
        "run.started",
        "run.input",
        "native.state",
        "run.terminal",
        "run.closed",
    ]
