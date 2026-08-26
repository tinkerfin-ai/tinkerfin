"""Public contracts for the request-scoped Deep Agents runtime package."""

from __future__ import annotations

import inspect

import tinkerfin
from tinkerfin import (
    AgUiEventStream,
    AgUiNativeStreamConfigurationError,
    AgUiResumeBindingError,
    AgUiSettlementTimeoutError,
    Identity,
    InMemoryRunCoordinator,
    NativeGraphRunStream,
    NativeStreamPart,
    RunCoordinator,
    SseBody,
    SsePayload,
    TinkerFin,
    join_task,
)


def test_top_level_exposes_the_stateless_runtime_contract() -> None:
    expected = {
        "AgUiSettlementTimeoutError",
        "AgUiNativeStreamConfigurationError",
        "AgUiEventStream",
        "AgUiResumeBinding",
        "AgUiResumeBindingError",
        "AgUiResumeCheckpoint",
        "AgUiResumeCheckpointObserver",
        "AgentMode",
        "DeepAgentAgUiRuntime",
        "DeepAgentAgUiResumeRuntime",
        "DeepAgentDefinition",
        "DeepAgentRuntime",
        "EventObserver",
        "Identity",
        "InMemoryRunCoordinator",
        "NativeGraphRunStream",
        "NativeStreamPart",
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
        "TINKERFIN_HITL_CONTRACT",
        "TinkerFinError",
        "TinkerFinErrorCode",
        "TinkerFinLifecycleError",
        "TinkerFinStreamProtocolError",
        "join_task",
    }

    assert set(tinkerfin.__all__) == expected
    assert all(getattr(tinkerfin, name) is not None for name in expected)
    assert all(
        value is not None
        for value in (
            AgUiNativeStreamConfigurationError,
            AgUiResumeBindingError,
            AgUiSettlementTimeoutError,
            AgUiEventStream,
            Identity,
            InMemoryRunCoordinator,
            NativeGraphRunStream,
            NativeStreamPart,
            RunCoordinator,
            SseBody,
            SsePayload,
            TinkerFin,
            join_task,
        )
    )
    assert issubclass(AgUiSettlementTimeoutError, TimeoutError)


def test_factory_has_no_application_resource_lifecycle() -> None:
    constructor = inspect.signature(TinkerFin).parameters

    assert tuple(constructor) == ("run_coordinator", "state_schema")
    assert not hasattr(TinkerFin, "__aenter__")
    assert not hasattr(TinkerFin, "__aexit__")


def test_low_level_source_contract_is_not_public() -> None:
    removed = {
        "AgUiNativeStreamConfig",
        "AgUiNativeStreamInvocation",
        "GraphRunStream",
        "NativeTinkerFinRun",
        "TinkerFinRun",
    }

    assert not hasattr(TinkerFin, "run")
    assert removed.isdisjoint(tinkerfin.__all__)
    assert all(not hasattr(tinkerfin, name) for name in removed)
