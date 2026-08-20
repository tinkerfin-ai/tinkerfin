# ruff: noqa: F403, F405
# Generated from locked dependencies by scripts/generate_stubs.py; do not edit signatures manually.
from collections.abc import Callable, Sequence
from typing import Any

from deepagents.graph import *

from tinkerfin_agui_adapter import Identity as Identity

from .agui_resume import AgUiResumeBinding as AgUiResumeBinding
from .coordination import InMemoryRunCoordinator as InMemoryRunCoordinator
from .coordination import RunCoordinator as RunCoordinator
from .deep_agent import DeepAgentAgUiRuntime as DeepAgentAgUiRuntime
from .deep_agent import DeepAgentDefinition as DeepAgentDefinition
from .deep_agent import DeepAgentRuntime as DeepAgentRuntime
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
from .errors import RunCoordinationUnavailableError as RunCoordinationUnavailableError
from .errors import TinkerFinError as TinkerFinError
from .errors import TinkerFinErrorCode as TinkerFinErrorCode
from .errors import TinkerFinLifecycleError as TinkerFinLifecycleError
from .errors import TinkerFinStreamProtocolError as TinkerFinStreamProtocolError
from .runtime import AgUiEventStream as AgUiEventStream
from .runtime import AgUiNativeStreamConfig as AgUiNativeStreamConfig
from .runtime import (
    AgUiNativeStreamConfigurationError as AgUiNativeStreamConfigurationError,
)
from .runtime import AgUiNativeStreamInvocation as AgUiNativeStreamInvocation
from .runtime import AgUiSettlementTimeoutError as AgUiSettlementTimeoutError
from .runtime import EventObserver as EventObserver
from .runtime import GraphRunStream as GraphRunStream
from .runtime import NativeGraphRunStream as NativeGraphRunStream
from .runtime import NativeStreamPart as NativeStreamPart
from .runtime import NativeTinkerFinRun as NativeTinkerFinRun
from .runtime import PartObserver as PartObserver
from .runtime import SseBody as SseBody
from .runtime import SseEventIdResolver as SseEventIdResolver
from .runtime import SseMapper as SseMapper
from .runtime import SsePayload as SsePayload
from .runtime import SsePreflight as SsePreflight
from .runtime import TinkerFin as _RuntimeTinkerFin
from .runtime import TinkerFinRun as TinkerFinRun

class TinkerFin(_RuntimeTinkerFin):
    def create_deep_agent(
        self,
        model: str | BaseChatModel | None = None,
        tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
        *,
        system_prompt: str | SystemMessage | None = None,
        middleware: Sequence[AgentMiddleware[StateT_co, ContextT]] = (),
        subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent] | None = None,
        skills: list[str] | None = None,
        memory: list[str] | None = None,
        permissions: list[FilesystemPermission] | None = None,
        backend: BackendProtocol | None = None,
        interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
        response_format: ResponseFormat[ResponseT]
        | type[ResponseT]
        | dict[str, Any]
        | None = None,
        state_schema: type[DeepAgentState] | None = None,
        context_schema: type[ContextT] | None = None,
        checkpointer: Checkpointer | None = None,
        store: BaseStore | None = None,
        debug: bool = False,
        name: str | None = None,
        cache: BaseCache | None = None,
    ) -> DeepAgentDefinition[ContextT]: ...

__all__: list[str]
