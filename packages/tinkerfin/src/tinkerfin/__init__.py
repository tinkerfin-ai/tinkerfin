"""Request-scoped Deep Agents runtime with native, AG-UI, and SSE streams."""

from typing import TYPE_CHECKING

from tinkerfin_contracts import ContextKind as ContextKind
from tinkerfin_contracts import RunIdentity as RunIdentity
from tinkerfin_native_stream import NativeStreamFrame as NativeStreamFrame

from ._call_observation import TraceContribution as TraceContribution
from ._call_observation import trace_contribution as trace_contribution
from ._hitl import TINKERFIN_HITL_CONTRACT as TINKERFIN_HITL_CONTRACT
from ._optional_dependencies import require_agui
from ._tasks import join_task as join_task
from .coordination import InMemoryRunCoordinator as InMemoryRunCoordinator
from .coordination import RunCoordinator as RunCoordinator
from .deep_agent import DeepAgentAgUiResumeRuntime as DeepAgentAgUiResumeRuntime
from .deep_agent import DeepAgentAgUiRuntime as DeepAgentAgUiRuntime
from .deep_agent import DeepAgentDefinition as DeepAgentDefinition
from .deep_agent import DeepAgentRuntime as DeepAgentRuntime
from .errors import AgUiResumeBindingError as AgUiResumeBindingError
from .errors import RedisLeaseError as RedisLeaseError
from .errors import RedisLeaseLifecycleError as RedisLeaseLifecycleError
from .errors import RedisLeaseProtocolError as RedisLeaseProtocolError
from .errors import RedisLeaseTimeoutError as RedisLeaseTimeoutError
from .errors import RedisLeaseUnavailableError as RedisLeaseUnavailableError
from .errors import RunCoordinationError as RunCoordinationError
from .errors import (
    RunCoordinationOwnershipLostError as RunCoordinationOwnershipLostError,
)
from .errors import RunCoordinationTimeoutError as RunCoordinationTimeoutError
from .errors import (
    RunCoordinationUnavailableError as RunCoordinationUnavailableError,
)
from .errors import RunObservationError as RunObservationError
from .errors import TinkerFinError as TinkerFinError
from .errors import TinkerFinErrorCode as TinkerFinErrorCode
from .errors import TinkerFinLifecycleError as TinkerFinLifecycleError
from .errors import TinkerFinStreamProtocolError as TinkerFinStreamProtocolError
from .native_driver import NativeStreamDriver as NativeStreamDriver
from .native_driver import ReasoningExtractor as ReasoningExtractor
from .plan import AgentMode as AgentMode
from .runtime import AgUiEventStream as AgUiEventStream
from .runtime import AgUiSettlementTimeoutError as AgUiSettlementTimeoutError
from .runtime import EventObserver as EventObserver
from .runtime import NativeGraphRunStream as NativeGraphRunStream
from .runtime import NativeStreamPart as NativeStreamPart
from .runtime import PartObserver as PartObserver
from .runtime import SseBody as SseBody
from .runtime import SseEventIdResolver as SseEventIdResolver
from .runtime import SseMapper as SseMapper
from .runtime import SsePayload as SsePayload
from .runtime import SsePreflight as SsePreflight
from .runtime import TinkerFin as TinkerFin
from .runtime_profile import (
    DeepAgentsFactoryPreparation as DeepAgentsFactoryPreparation,
)
from .runtime_profile import DeepAgentsRuntimeProfile as DeepAgentsRuntimeProfile
from .runtime_profile import DeepAgentsV2RuntimeProfile as DeepAgentsV2RuntimeProfile
from .runtime_profile import DeepAgentsV3RuntimeProfile as DeepAgentsV3RuntimeProfile

if TYPE_CHECKING:
    from .agui_resume import AgUiResumeBinding as AgUiResumeBinding
    from .agui_resume import AgUiResumeCheckpoint as AgUiResumeCheckpoint
    from .agui_resume import (
        AgUiResumeCheckpointObserver as AgUiResumeCheckpointObserver,
    )
    from .agui_resume import (
        AgUiResumeInitializationFailureObserver as AgUiResumeInitializationFailureObserver,
    )
    from .agui_resume import AgUiResumeRequest as AgUiResumeRequest

_AGUI_RESUME_EXPORTS = frozenset(
    {
        "AgUiResumeBinding",
        "AgUiResumeCheckpoint",
        "AgUiResumeCheckpointObserver",
        "AgUiResumeInitializationFailureObserver",
        "AgUiResumeRequest",
    }
)


def __getattr__(name: str) -> object:
    """Load AG-UI resume contracts only when their optional extra is present."""

    if name not in _AGUI_RESUME_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    require_agui()
    from . import agui_resume

    value = getattr(agui_resume, name)
    globals()[name] = value
    return value


__all__ = [
    "TINKERFIN_HITL_CONTRACT",
    "AgUiEventStream",
    "AgUiResumeBinding",
    "AgUiResumeBindingError",
    "AgUiResumeCheckpoint",
    "AgUiResumeCheckpointObserver",
    "AgUiResumeInitializationFailureObserver",
    "AgUiResumeRequest",
    "AgUiSettlementTimeoutError",
    "AgentMode",
    "ContextKind",
    "DeepAgentAgUiResumeRuntime",
    "DeepAgentAgUiRuntime",
    "DeepAgentDefinition",
    "DeepAgentRuntime",
    "DeepAgentsFactoryPreparation",
    "DeepAgentsRuntimeProfile",
    "DeepAgentsV2RuntimeProfile",
    "DeepAgentsV3RuntimeProfile",
    "EventObserver",
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
    "RunCoordinationError",
    "RunCoordinationOwnershipLostError",
    "RunCoordinationTimeoutError",
    "RunCoordinationUnavailableError",
    "RunCoordinator",
    "RunIdentity",
    "RunObservationError",
    "SseBody",
    "SseEventIdResolver",
    "SseMapper",
    "SsePayload",
    "SsePreflight",
    "TinkerFin",
    "TinkerFinError",
    "TinkerFinErrorCode",
    "TinkerFinLifecycleError",
    "TinkerFinStreamProtocolError",
    "TraceContribution",
    "join_task",
    "trace_contribution",
]
