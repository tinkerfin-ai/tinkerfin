"""Typed public Runtime and Definition contracts generated from locked source."""

# Generated from locked dependencies by scripts/generate_stubs.py; do not edit signatures manually.
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Generic, Literal, overload

from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.pregel.main import (
    All,
    DeprecatedKwargs,
    Durability,
    RunControl,
    StreamMode,
)
from langgraph.types import Command
from langgraph.typing import ContextT
from typing_extensions import Unpack

from tinkerfin_contracts import RunIdentity as RunIdentity

from .agui_resume import (
    AgUiResumeBinding,
    AgUiResumeCheckpointObserver,
    AgUiResumeInitializationFailureObserver,
    AgUiResumeRequest,
)
from .plan import AgentMode
from .runtime import AgUiEventStream, EventObserver, NativeGraphRunStream, PartObserver

class DeepAgentGraph(
    Runnable[InputAgentState | Command[object] | None, Mapping[str, object]]
):
    """Complete reusable async native or Plan-capable Graph."""

    def invoke(
        self,
        input: InputAgentState | Command[object] | None,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Mapping[str, object]: ...
    async def ainvoke(
        self,
        input: InputAgentState | Command[object] | None,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Mapping[str, object]: ...
    def astream(
        self,
        input: InputAgentState | Command[object] | None,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Mapping[str, object]]: ...

class DeepAgentRuntime(Generic[ContextT]):
    """Single-request Runtime preserving the native LangGraph object stream."""

    def astream(
        self,
        input: InputAgentState | Command | None,  # pyright: ignore[reportMissingTypeArgument,reportUnknownParameterType]
        config: RunnableConfig | None = None,
        *,
        context: ContextT | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        subgraphs: Literal[True] = True,
        debug: bool | None = None,
        version: Literal["v2"] = "v2",
        **kwargs: Unpack[DeprecatedKwargs],
    ) -> NativeGraphRunStream:
        """Iterate one Profile-bound Graph as original Native objects.

        Args:
            input: Ordinary Graph state or a framework-owned continuation command.
            config: Optional LangGraph execution configuration.
            context: Optional Graph context declared by the Definition.
            stream_mode: Optional supported extra modes; the Driver always adds messages,
                tasks, and values exactly once.
            print_mode: Optional upstream debug printing that does not change yielded data.
            output_keys: Must remain ``None`` so state and interrupt evidence is complete.
            interrupt_before: Optional upstream node interrupts.
            interrupt_after: Optional upstream node interrupts.
            durability: Optional upstream checkpoint durability.
            control: Optional cooperative LangGraph run control.
            subgraphs: Must remain ``True`` for complete namespace provenance.
            debug: Optional upstream debug mode.
            version: Must remain the selected v2 Profile's fixed ``"v2"`` value.
            **kwargs: Locked LangGraph compatibility keywords accepted by the upstream call.

        Returns:
            A single-use stream yielding original objects after validation, Observation, and
            callback settlement.

        Raises:
            TypeError: Input or a fixed Profile option has the wrong type.
            ValueError: Required modes or fixed Profile options are incomplete or conflicting.
            TinkerFinError: Runtime, Observation, coordination, or stream validation fails.
        """
        ...

class DeepAgentAgUiRuntime(Generic[ContextT]):
    """Single-request AG-UI Runtime requiring one explicit Graph input."""

    def astream(
        self,
        input: InputAgentState,
        config: RunnableConfig | None = None,
        *,
        context: ContextT | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        subgraphs: Literal[True] = True,
        debug: bool | None = None,
        version: Literal["v2"] = "v2",
        **kwargs: Unpack[DeprecatedKwargs],
    ) -> AgUiEventStream:
        """Iterate one ordinary Graph request as lifecycle-safe AG-UI events.

        Args:
            input: Explicit ordinary Graph state selected by the host.
            config: Optional LangGraph execution configuration.
            context: Optional Graph context declared by the Definition.
            stream_mode: Optional supported extra modes; required semantic modes are automatic.
            print_mode: Optional upstream debug printing that does not alter AG-UI output.
            output_keys: Must remain ``None`` so final synchronization is complete.
            interrupt_before: Optional upstream node interrupts.
            interrupt_after: Optional upstream node interrupts.
            durability: Optional upstream checkpoint durability.
            control: Optional cooperative LangGraph run control.
            subgraphs: Must remain ``True`` for complete subagent provenance.
            debug: Optional upstream debug mode.
            version: Must remain the selected v2 Profile's fixed ``"v2"`` value.
            **kwargs: Locked LangGraph compatibility keywords accepted by the upstream call.

        Returns:
            A single-use AG-UI event stream that owns and closes the Native source.

        Raises:
            TypeError: Input or a fixed Profile option has the wrong type.
            ValueError: Required modes or fixed Profile options are incomplete or conflicting.
            TinkerFinError: Runtime, Observation, conversion, or settlement fails.
        """
        ...

class DeepAgentAgUiResumeRuntime(Generic[ContextT]):
    """Single-request AG-UI Runtime with framework-owned resume input."""

    def astream(
        self,
        *,
        config: RunnableConfig | None = None,
        context: ContextT | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Literal["sync"] | None = None,
        control: RunControl | None = None,
        subgraphs: Literal[True] = True,
        debug: bool | None = None,
        version: Literal["v2"] = "v2",
        **kwargs: Unpack[DeprecatedKwargs],
    ) -> AgUiEventStream:
        """Continue one checkpointed request as AG-UI events.

        Args:
            config: Optional LangGraph execution configuration for the same checkpoint thread.
            context: Optional Graph context declared by the Definition.
            stream_mode: Optional supported extra modes; required semantic modes are automatic.
            print_mode: Optional upstream debug printing that does not alter AG-UI output.
            output_keys: Must remain ``None`` so final synchronization is complete.
            interrupt_before: Optional upstream node interrupts.
            interrupt_after: Optional upstream node interrupts.
            durability: Must remain ``None`` or ``"sync"`` for durable resume markers.
            control: Optional cooperative LangGraph run control.
            subgraphs: Must remain ``True`` for complete subagent provenance.
            debug: Optional upstream debug mode.
            version: Must remain the selected v2 Profile's fixed ``"v2"`` value.
            **kwargs: Locked LangGraph compatibility keywords accepted by the upstream call.

        Returns:
            A single-use AG-UI event stream with framework-owned resume input.

        Raises:
            TypeError: A fixed Profile option has the wrong type.
            ValueError: Required modes, durability, or fixed Profile options conflict.
            TinkerFinError: Resume validation, Graph continuation, conversion, or settlement
                fails.
        """
        ...

class DeepAgentDefinition(Generic[ContextT]):
    """Store one Graph build call and create a fresh Graph for every execution."""

    async def create_graph(self, *, mode: AgentMode | None = None) -> DeepAgentGraph:
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
            BaseException: Profile or Planning Graph construction fails."""
        ...
    def _belongs_to(self, family: object) -> bool:
        """Return whether this Definition came from one configured factory family."""
        ...
    def _resume_checkpointer(self) -> object | None:
        """Return the effective borrowed saver without transferring ownership."""
        ...
    async def _open_native_run(
        self,
        *,
        identity: RunIdentity,
        input: InputAgentState | Command[object] | None,
        mode: AgentMode | None,
        config: RunnableConfig | None,
        context: object | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        stream_options: Mapping[str, object],
    ) -> NativeGraphRunStream:
        """Build once and return one managed native stream for the common facade."""
        ...
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
        on_resume_initialization_failed: AgUiResumeInitializationFailureObserver | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        on_part: PartObserver[Mapping[str, object]] | None,
        on_event: EventObserver | None,
        stream_options: Mapping[str, object],
    ) -> AgUiEventStream:
        """Build once and return one managed AG-UI stream for the common facade."""
        ...
    def new(
        self,
        *,
        identity: RunIdentity,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
    ) -> DeepAgentRuntime[ContextT]:
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
            ValueError: The selected mode is not available on this Definition."""
        ...
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
            AgUiResumeBindingError: Client coverage or decision validation fails."""
        ...
    @overload
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
        resume: None = None,
        on_resume_checkpointed: None = None,
        on_resume_initialization_failed: None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[ContextT]:
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
                subagent contract."""
        ...
    @overload
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
        resume: AgUiResumeBinding,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None = None,
        on_resume_initialization_failed: AgUiResumeInitializationFailureObserver
        | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiResumeRuntime[ContextT]:
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
                subagent contract."""
        ...

CREATE_DEEP_AGENT: object
