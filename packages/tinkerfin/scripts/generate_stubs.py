"""Generate explicit PyCharm-facing signatures from locked upstream source."""

from __future__ import annotations

import argparse
import ast
import copy
import inspect
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path

from deepagents.graph import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from tinkerfin.deep_agent import DeepAgentDefinition
from tinkerfin.runtime import TinkerFin as RuntimeTinkerFin

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_PACKAGE = _PACKAGE_ROOT / "src/tinkerfin"
_DEEP_AGENT_STUB = _PACKAGE / "deep_agent.pyi"
_INIT_STUB = _PACKAGE / "__init__.pyi"

_NATIVE_ASTREAM_DOC = """Iterate one Profile-bound Graph as original Native objects.

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

_AGUI_ASTREAM_DOC = """Iterate one ordinary Graph request as lifecycle-safe AG-UI events.

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

_RESUME_ASTREAM_DOC = """Continue one checkpointed request as AG-UI events.

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


class _RenameTypes(ast.NodeTransformer):
    def __init__(self, replacements: Mapping[str, str]) -> None:
        self._replacements = replacements

    def visit_Name(self, node: ast.Name) -> ast.expr:
        replacement = self._replacements.get(node.id)
        if replacement is None:
            return node
        return ast.copy_location(ast.parse(replacement, mode="eval").body, node)


def _method(
    function: Callable[..., object],
    *,
    add_self: bool = False,
    is_async: bool = False,
    canonical_stream_profile: bool = False,
    docstring: str | None = None,
    replacements: Mapping[str, str] | None = None,
    return_type: str,
) -> str:
    module = ast.parse(textwrap.dedent(inspect.getsource(function)))
    source = next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    arguments = copy.deepcopy(source.args)
    if canonical_stream_profile:
        for index, argument in enumerate(arguments.kwonlyargs):
            if argument.arg == "output_keys":
                argument.annotation = ast.parse("None", mode="eval").body
                arguments.kw_defaults[index] = ast.Constant(value=None)
            elif argument.arg == "subgraphs":
                argument.annotation = ast.parse("Literal[True]", mode="eval").body
                arguments.kw_defaults[index] = ast.Constant(value=True)
            elif argument.arg == "version":
                argument.annotation = ast.parse('Literal["v2"]', mode="eval").body
                arguments.kw_defaults[index] = ast.Constant(value="v2")
    if add_self:
        arguments.args.insert(0, ast.arg(arg="self"))
    # Generated stream attributes are synchronous factories even when their upstream
    # signature comes from an async-generator function. Explicit coroutine methods
    # opt in so the stub preserves their required ``await`` boundary.
    placeholder = ast.parse(
        "async def generated(): ..." if is_async else "def generated(): ..."
    ).body[0]
    assert isinstance(placeholder, (ast.FunctionDef, ast.AsyncFunctionDef))
    placeholder.name = source.name
    placeholder.args = arguments
    placeholder.decorator_list = []
    placeholder.returns = ast.parse(return_type, mode="eval").body
    placeholder.type_comment = None
    resolved_docstring = docstring or ast.get_docstring(source)
    if resolved_docstring is not None:
        placeholder.body.insert(
            0,
            ast.Expr(value=ast.Constant(value=resolved_docstring)),
        )
    if replacements:
        placeholder = _RenameTypes(replacements).visit(placeholder)
    ast.fix_missing_locations(placeholder)
    return textwrap.indent(ast.unparse(placeholder), "    ")


def _format(content: str, *, target: Path) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(_REPOSITORY_ROOT / "pyproject.toml"),
            "--stdin-filename",
            target.name,
            "-",
        ],
        cwd=_REPOSITORY_ROOT,
        input=content,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _with_pyright_ignores(
    content: str,
    ignores: Mapping[str, tuple[str, ...]],
) -> str:
    """Annotate locked upstream generic gaps without changing public signatures."""

    lines = content.splitlines()
    for index, line in enumerate(lines):
        for marker, rules in ignores.items():
            if marker in line:
                lines[index] = f"{line}  # pyright: ignore[{','.join(rules)}]"
                break
    return "\n".join(lines) + "\n"


def _render_deep_agent_stub() -> str:
    astream_arguments = {"InputT": "InputAgentState"}
    agui_astream = _method(
        CompiledStateGraph.astream,
        canonical_stream_profile=True,
        docstring=_AGUI_ASTREAM_DOC,
        replacements=astream_arguments,
        return_type="AgUiEventStream",
    ).replace(
        "input: InputAgentState | Command | None,",
        "input: InputAgentState,",
    )
    new_agui_doc = textwrap.indent(
        f'"""{inspect.getdoc(DeepAgentDefinition.new_agui)}"""',
        "        ",
    )
    resume_astream_doc = textwrap.indent(
        f'"""{_RESUME_ASTREAM_DOC}"""',
        "        ",
    )
    content = f'''"""Typed public Runtime and Definition contracts generated from locked source."""

# Generated from locked dependencies by scripts/generate_stubs.py; do not edit signatures manually.
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Generic, Literal, overload

from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.pregel.main import All, DeprecatedKwargs, Durability, RunControl, StreamMode
from langgraph.types import Command
from langgraph.typing import ContextT
from typing_extensions import Unpack

from tinkerfin_contracts import RunIdentity as RunIdentity

from .agui_resume import AgUiResumeBinding, AgUiResumeCheckpointObserver, AgUiResumeInitializationFailureObserver, AgUiResumeRequest
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

{
        _method(
            CompiledStateGraph.astream,
            canonical_stream_profile=True,
            docstring=_NATIVE_ASTREAM_DOC,
            replacements=astream_arguments,
            return_type="NativeGraphRunStream",
        )
    }

class DeepAgentAgUiRuntime(Generic[ContextT]):
    """Single-request AG-UI Runtime requiring one explicit Graph input."""

{agui_astream}

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
{resume_astream_doc}
        ...

class DeepAgentDefinition(Generic[ContextT]):
    """Store one Graph build call and create a fresh Graph for every execution."""

{_method(DeepAgentDefinition.create_graph, is_async=True, return_type="DeepAgentGraph")}
{_method(DeepAgentDefinition._belongs_to, return_type="bool")}
{_method(DeepAgentDefinition._resume_checkpointer, return_type="object | None")}
{
        _method(
            DeepAgentDefinition._open_native_run,
            is_async=True,
            replacements={"_GraphInput": "InputAgentState | Command[object] | None"},
            return_type="NativeGraphRunStream",
        )
    }
{
        _method(
            DeepAgentDefinition._open_agui_run,
            is_async=True,
            return_type="AgUiEventStream",
        )
    }
{_method(DeepAgentDefinition.new, return_type="DeepAgentRuntime[ContextT]")}
{
        _method(
            DeepAgentDefinition.prepare_agui_resume,
            is_async=True,
            return_type="AgUiResumeBinding",
        )
    }
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
{new_agui_doc}
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
        on_resume_initialization_failed: AgUiResumeInitializationFailureObserver | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiResumeRuntime[ContextT]:
{new_agui_doc}
        ...

CREATE_DEEP_AGENT: object
'''
    formatted = _format(content, target=_DEEP_AGENT_STUB)
    return _with_pyright_ignores(
        formatted,
        {
            "input: InputAgentState | Command | None,": (
                "reportMissingTypeArgument",
                "reportUnknownParameterType",
            ),
        },
    )


def _render_init_stub() -> str:
    content = f'''"""Typed root facade generated from the current Runtime implementation."""

# Generated from locked dependencies by scripts/generate_stubs.py; do not edit signatures manually.
from collections.abc import Callable, Sequence
from typing import Any

from deepagents import (
    AsyncSubAgent,
    CompiledSubAgent,
    DeepAgentState,
    FilesystemPermission,
    SubAgent,
)
from deepagents.backends import BackendProtocol
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig
from langchain.agents.middleware.types import ResponseT, StateT_co
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.cache.base import BaseCache
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer
from langgraph.typing import ContextT

from tinkerfin_contracts import ContextKind as ContextKind
from tinkerfin_contracts import RunIdentity as RunIdentity
from tinkerfin_contracts import RuntimeObserver as _RuntimeObserver
from tinkerfin_native_stream import NativeStreamFrame as NativeStreamFrame

from ._call_observation import TraceContribution as TraceContribution
from ._call_observation import trace_contribution as trace_contribution
from ._hitl import TINKERFIN_HITL_CONTRACT as TINKERFIN_HITL_CONTRACT
from ._tasks import join_task as join_task
from .agui_resume import AgUiResumeBinding as AgUiResumeBinding
from .agui_resume import AgUiResumeCheckpoint as AgUiResumeCheckpoint
from .agui_resume import AgUiResumeCheckpointObserver as AgUiResumeCheckpointObserver
from .agui_resume import AgUiResumeInitializationFailureObserver as AgUiResumeInitializationFailureObserver
from .agui_resume import AgUiResumeRequest as AgUiResumeRequest
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
from .errors import RunCoordinationOwnershipLostError as RunCoordinationOwnershipLostError
from .errors import RunCoordinationTimeoutError as RunCoordinationTimeoutError
from .errors import RunCoordinationUnavailableError as RunCoordinationUnavailableError
from .errors import RunObservationError as RunObservationError
from .errors import TinkerFinError as TinkerFinError
from .errors import TinkerFinErrorCode as TinkerFinErrorCode
from .errors import TinkerFinLifecycleError as TinkerFinLifecycleError
from .errors import TinkerFinStreamProtocolError as TinkerFinStreamProtocolError
from .native_driver import DeepAgentsV2StreamDriver as DeepAgentsV2StreamDriver
from .native_driver import DeepAgentsV3StreamDriver as DeepAgentsV3StreamDriver
from .native_driver import DeepSeekReasoningExtractor as DeepSeekReasoningExtractor
from .native_driver import NativeStreamDriver as NativeStreamDriver
from .native_driver import ReasoningExtractor as ReasoningExtractor
from .plan import AgentMode as AgentMode
from .plan import ClarificationFormBase as _ClarificationFormBase
from .plan import ClarificationType as _ClarificationType
from .plan import DefaultClarificationForm as _DefaultClarificationForm
from .plan import PlanContentModel as _PlanContentModel
from .plan import PlanReviewAction as _PlanReviewAction
from .plan import StructuredPlanContent as _StructuredPlanContent
from .plan._config import DEFAULT_ALLOWED_REVIEW_ACTIONS as _DEFAULT_ALLOWED_REVIEW_ACTIONS
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
from .runtime import TinkerFin as _RuntimeTinkerFin
from .runtime_profile import DeepAgentsFactoryPreparation as DeepAgentsFactoryPreparation
from .runtime_profile import DeepAgentsRuntimeProfile as DeepAgentsRuntimeProfile
from .runtime_profile import DeepAgentsV2RuntimeProfile as DeepAgentsV2RuntimeProfile
from .runtime_profile import DeepAgentsV3RuntimeProfile as DeepAgentsV3RuntimeProfile

class TinkerFin(_RuntimeTinkerFin):
    """Configure immutable Runtime, Profile, Plan, and Observation capabilities."""

{
        _method(
            RuntimeTinkerFin.observe,
            replacements={"RuntimeObserver": "_RuntimeObserver"},
            return_type="TinkerFin",
        )
    }
{
        _method(
            RuntimeTinkerFin.plan,
            replacements={
                "ClarificationFormBase": "_ClarificationFormBase",
                "ClarificationType": "_ClarificationType",
                "DefaultClarificationForm": "_DefaultClarificationForm",
                "PlanContentModel": "_PlanContentModel",
                "PlanReviewAction": "_PlanReviewAction",
                "StructuredPlanContent": "_StructuredPlanContent",
                "DEFAULT_ALLOWED_REVIEW_ACTIONS": "_DEFAULT_ALLOWED_REVIEW_ACTIONS",
            },
            return_type="TinkerFin",
        )
    }
{
        _method(
            create_deep_agent,
            add_self=True,
            return_type="DeepAgentDefinition[ContextT]",
        )
    }

__all__: list[str]
'''
    formatted = _format(content, target=_INIT_STUB)
    return _with_pyright_ignores(
        formatted,
        {
            "tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,": (
                "reportMissingTypeArgument",
                "reportUnknownParameterType",
            ),
            "checkpointer: Checkpointer | None = None,": (
                "reportUnknownParameterType",
            ),
            "cache: BaseCache | None = None,": (
                "reportMissingTypeArgument",
                "reportUnknownParameterType",
            ),
        },
    )


def render_stubs() -> dict[Path, str]:
    return {
        _DEEP_AGENT_STUB: _render_deep_agent_stub(),
        _INIT_STUB: _render_init_stub(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check committed stubs without writing files",
    )
    options = parser.parse_args()
    rendered = render_stubs()
    if options.check:
        drift = [
            path
            for path, content in rendered.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if drift:
            for path in drift:
                print(f"generated stub drift: {path}", file=sys.stderr)
            return 1
        return 0
    for path, content in rendered.items():
        path.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
