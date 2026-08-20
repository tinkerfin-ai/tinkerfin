"""Public contracts for the stateless Graph runtime package."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import assert_type

import pytest
from pydantic import BaseModel

import tinkerfin
from tinkerfin import (
    AgUiEventStream,
    AgUiNativeStreamConfig,
    AgUiNativeStreamConfigurationError,
    AgUiNativeStreamInvocation,
    AgUiSettlementTimeoutError,
    GraphRunStream,
    Identity,
    InMemoryRunCoordinator,
    NativeGraphRunStream,
    NativeStreamPart,
    NativeTinkerFinRun,
    RunCoordinator,
    SseBody,
    SsePayload,
    TinkerFin,
    TinkerFinRun,
)


@dataclass(frozen=True, slots=True)
class _DataclassPart:
    value: int


class _ModelPart(BaseModel):
    value: int


def test_top_level_exposes_the_stateless_runtime_contract() -> None:
    expected = {
        "AgUiSettlementTimeoutError",
        "AgUiNativeStreamConfig",
        "AgUiNativeStreamConfigurationError",
        "AgUiNativeStreamInvocation",
        "AgUiEventStream",
        "AgUiResumeBinding",
        "DeepAgentAgUiRuntime",
        "DeepAgentDefinition",
        "DeepAgentRuntime",
        "EventObserver",
        "GraphRunStream",
        "Identity",
        "InMemoryRunCoordinator",
        "NativeGraphRunStream",
        "NativeStreamPart",
        "NativeTinkerFinRun",
        "PartObserver",
        "RedisLeaseError",
        "RedisLeaseLifecycleError",
        "RedisLeaseProtocolError",
        "RedisLeaseTimeoutError",
        "RedisLeaseUnavailableError",
        "RunCoordinator",
        "RunCoordinationError",
        "RunCoordinationOwnershipLostError",
        "RunCoordinationTimeoutError",
        "RunCoordinationUnavailableError",
        "SseBody",
        "SseEventIdResolver",
        "SseMapper",
        "SsePayload",
        "SsePreflight",
        "TinkerFin",
        "TinkerFinError",
        "TinkerFinErrorCode",
        "TinkerFinLifecycleError",
        "TinkerFinRun",
        "TinkerFinStreamProtocolError",
    }

    assert set(tinkerfin.__all__) == expected
    assert all(getattr(tinkerfin, name) is not None for name in expected)
    assert all(
        value is not None
        for value in (
            AgUiNativeStreamConfig,
            AgUiNativeStreamConfigurationError,
            AgUiNativeStreamInvocation,
            AgUiSettlementTimeoutError,
            AgUiEventStream,
            GraphRunStream,
            Identity,
            InMemoryRunCoordinator,
            NativeGraphRunStream,
            NativeStreamPart,
            NativeTinkerFinRun,
            RunCoordinator,
            SseBody,
            SsePayload,
            TinkerFin,
            TinkerFinRun,
        )
    )
    assert issubclass(AgUiSettlementTimeoutError, TimeoutError)


def test_factory_has_no_application_resource_lifecycle() -> None:
    constructor = inspect.signature(TinkerFin).parameters

    assert tuple(constructor) == ("run_coordinator",)
    assert not hasattr(TinkerFin, "__aenter__")
    assert not hasattr(TinkerFin, "__aexit__")


def test_source_binding_keeps_graph_arguments_at_the_factory_call_site() -> None:
    run_parameters = inspect.signature(TinkerFin.run).parameters
    native_parameters = inspect.signature(TinkerFinRun.astream).parameters
    agui_parameters = inspect.signature(TinkerFinRun.astream_agui).parameters

    assert tuple(run_parameters) == (
        "self",
        "source_factory",
        "identity",
        "on_part",
    )
    assert tuple(native_parameters) == ("self",)
    assert tuple(agui_parameters) == (
        "self",
        "timeout",
        "settlement_timeout",
        "expose_reasoning_events",
        "expose_subagent_events",
        "prior_tool_call_ids",
        "on_event",
    )
    assert not hasattr(TinkerFin, "run_agui")
    assert not hasattr(TinkerFinRun, "agui")
    assert not hasattr(TinkerFinRun, "sse")


def test_generic_object_streams_do_not_claim_a_native_codec_profile() -> None:
    async def dataclass_parts() -> AsyncIterator[_DataclassPart]:
        yield _DataclassPart(value=1)

    async def model_parts() -> AsyncIterator[_ModelPart]:
        yield _ModelPart(value=2)

    dataclass_run = TinkerFin().run(dataclass_parts)
    model_run = TinkerFin().run(model_parts)
    assert_type(dataclass_run, TinkerFinRun[_DataclassPart])
    assert_type(model_run, TinkerFinRun[_ModelPart])

    streams = (dataclass_run.astream(), model_run.astream())

    for stream in streams:
        assert not hasattr(stream, "messaging_codec_profile")
        assert not hasattr(stream, "messaging_source_type")
        assert not hasattr(stream, "messaging_replay_type")


def test_strict_native_stream_exposes_a_complete_public_profile() -> None:
    async def parts(**options: object) -> AsyncIterator[Mapping[str, object]]:
        del options
        yield {
            "type": "values",
            "ns": (),
            "data": {"answer": 42},
            "interrupts": (),
        }

    invocation = AgUiNativeStreamConfig().bind(parts)
    identity = Identity(threadId="thread-1", runId="run-1")
    run = TinkerFin().run(invocation, identity=identity)
    stream = run.astream()

    assert_type(run, NativeTinkerFinRun)
    assert_type(stream, NativeGraphRunStream)

    native_run_type = getattr(tinkerfin, "NativeTinkerFinRun", None)
    native_stream_type = getattr(tinkerfin, "NativeGraphRunStream", None)
    assert native_run_type is not None
    assert native_stream_type is not None
    assert isinstance(run, native_run_type)
    assert isinstance(stream, native_stream_type)
    assert stream.messaging_codec_profile == "langgraph.stream-part.v2.v1"
    assert stream.messaging_source_type is Mapping
    assert stream.messaging_replay_type is NativeStreamPart
    assert stream.messaging_identity is identity
    with pytest.raises(AttributeError):
        setattr(stream, "messaging_codec_profile", "vendor.changed.v1")
