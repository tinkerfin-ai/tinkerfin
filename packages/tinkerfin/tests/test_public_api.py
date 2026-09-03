"""Public contracts for the request-scoped Deep Agents runtime package."""

from __future__ import annotations

import inspect

import tinkerfin
from tinkerfin import (
    AgUiEventStream,
    AgUiResumeBindingError,
    AgUiSettlementTimeoutError,
    DeepAgentsFactoryPreparation,
    DeepAgentsRuntimeProfile,
    DeepAgentsV2RuntimeProfile,
    DeepAgentsV2StreamDriver,
    DeepAgentsV3RuntimeProfile,
    DeepAgentsV3StreamDriver,
    DeepSeekReasoningExtractor,
    InMemoryRunCoordinator,
    NativeGraphRunStream,
    NativeStreamDriver,
    NativeStreamFrame,
    NativeStreamPart,
    ReasoningExtractor,
    RunCoordinator,
    RunIdentity,
    RunObservationError,
    SseBody,
    SsePayload,
    TinkerFin,
    join_task,
)


def test_top_level_exposes_the_stateless_runtime_contract() -> None:
    expected = {
        "AgUiSettlementTimeoutError",
        "AgUiEventStream",
        "AgUiResumeBinding",
        "AgUiResumeBindingError",
        "AgUiResumeCheckpoint",
        "AgUiResumeCheckpointObserver",
        "AgUiResumeInitializationFailureObserver",
        "AgUiResumeRequest",
        "AgentMode",
        "ContextKind",
        "DeepAgentAgUiRuntime",
        "DeepAgentAgUiResumeRuntime",
        "DeepAgentDefinition",
        "DeepAgentRuntime",
        "DeepAgentsFactoryPreparation",
        "DeepAgentsRuntimeProfile",
        "DeepAgentsV2RuntimeProfile",
        "DeepAgentsV2StreamDriver",
        "DeepAgentsV3RuntimeProfile",
        "DeepAgentsV3StreamDriver",
        "DeepSeekReasoningExtractor",
        "EventObserver",
        "RunIdentity",
        "RunObservationError",
        "InMemoryRunCoordinator",
        "NativeGraphRunStream",
        "NativeStreamDriver",
        "NativeStreamFrame",
        "NativeStreamPart",
        "PartObserver",
        "ReasoningExtractor",
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
        "TraceContribution",
        "join_task",
        "trace_contribution",
    }

    assert set(tinkerfin.__all__) == expected
    assert all(getattr(tinkerfin, name) is not None for name in expected)
    assert all(
        value is not None
        for value in (
            AgUiResumeBindingError,
            AgUiSettlementTimeoutError,
            AgUiEventStream,
            DeepAgentsFactoryPreparation,
            DeepAgentsRuntimeProfile,
            DeepAgentsV2RuntimeProfile,
            DeepAgentsV2StreamDriver,
            DeepAgentsV3RuntimeProfile,
            DeepAgentsV3StreamDriver,
            DeepSeekReasoningExtractor,
            RunIdentity,
            RunObservationError,
            InMemoryRunCoordinator,
            NativeGraphRunStream,
            NativeStreamDriver,
            NativeStreamFrame,
            NativeStreamPart,
            ReasoningExtractor,
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

    assert tuple(constructor) == (
        "checkpointer",
        "run_coordinator",
        "state_schema",
        "runtime_profile",
    )
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
