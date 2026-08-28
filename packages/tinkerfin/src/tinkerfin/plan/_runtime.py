"""Request-local routing between standalone Planning and native Deep Agent graphs."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Protocol, TypeAlias, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from .._agui_lineage import (
    NATIVE_CHECKPOINT_ROLE,
    PLANNING_CHECKPOINT_ROLE,
    RUN_ID_METADATA_KEY,
    RUNTIME_PROFILE_METADATA_KEY,
    resolve_agui_native_run_head,
    resolve_agui_thread_head,
)
from .._agui_lineage_state import (
    LINEAGE_STATE_KEY,
    RESUME_MARKER_STATE_KEY,
    LineageRole,
    lineage_marker_with_role,
)
from ._clarification import (
    pending_contract_digest,
    restore_form,
    validate_and_normalize_response,
)
from ._config import PlanOptions
from ._content import PlanContentBinding
from ._state import (
    PLAN_CHECKPOINT_RUN_ID,
    PLAN_SCHEMA_FINGERPRINT_KEY,
    PLAN_STATE_KEY,
    read_plan_state,
)
from ._workflow import PlanningWorkflowGraph
from .errors import PlanModeConfigurationError, PlanStateConflictError
from .models import (
    MarkdownPlanContent,
    PlanContentModel,
    PlanHandoffPhase,
    PlanState,
    PlanStatus,
)

_APPROVED_PLAN_MARKER = "<tinkerfin-approved-plan"
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


class _GraphRuntime(Protocol):
    @property
    def checkpointer(self) -> object: ...

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


class _SignatureCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


_COMPILED_ASTREAM = cast(
    _SignatureCallable,
    CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
)


def _config(bound: inspect.BoundArguments) -> RunnableConfig:
    value = bound.arguments.get("config")
    if value is None:
        return RunnableConfig()
    if not isinstance(value, Mapping):
        raise TypeError("config must be a mapping or None")
    return cast(RunnableConfig, dict(cast(Mapping[str, object], value)))


def _select_checkpoint(
    config: RunnableConfig,
    checkpoint_id: str,
) -> RunnableConfig:
    selected = cast(RunnableConfig, dict(config))
    configurable = dict(selected.get("configurable", {}))
    configurable["checkpoint_id"] = checkpoint_id
    selected["configurable"] = configurable
    return selected


def _native_checkpoint_id_for_plan(
    plan: PlanState[PlanContentModel] | None,
) -> str | None:
    handoff = None if plan is None else plan.handoff
    return (
        None
        if handoff is None
        else handoff.completed_checkpoint_id or handoff.native_checkpoint_id
    )


async def _native_snapshot_for_plan(
    native: _GraphRuntime,
    config: RunnableConfig,
    plan: PlanState[PlanContentModel] | None,
) -> tuple[StateSnapshot, RunnableConfig]:
    checkpoint_id = _native_checkpoint_id_for_plan(plan)
    selected = (
        config
        if checkpoint_id is None
        else _select_checkpoint(
            config,
            checkpoint_id,
        )
    )
    return await native.aget_state(selected), selected


def _lineage_update_for_role(
    state: Mapping[str, object],
    *,
    role: LineageRole,
    required: bool,
) -> dict[str, object]:
    """Change Graph role while carrying an accepted resume marker across handoff."""

    raw_marker = state.get(LINEAGE_STATE_KEY)
    if raw_marker is None:
        if required:
            raise PlanStateConflictError("Plan state lost its private lineage marker")
        return {}
    try:
        marker = lineage_marker_with_role(raw_marker, role=role)
    except ValueError as error:
        raise PlanStateConflictError(
            "Plan state has an invalid lineage marker"
        ) from error
    update: dict[str, object] = {LINEAGE_STATE_KEY: marker}
    if RESUME_MARKER_STATE_KEY in state:
        update[RESUME_MARKER_STATE_KEY] = state[RESUME_MARKER_STATE_KEY]
    return update


def _input_with_lineage_role(
    value: object,
    *,
    role: LineageRole,
    required: bool,
) -> object:
    """Rewrite only the private lineage role on state or resume input.

    Command routing, resume payload, and every host state field remain unchanged. A
    decision-only Command is valid after the two-phase Runtime has durably staged the
    role marker; Commands that still carry a state update must contain an existing
    marker rather than asking this router to invent lineage.
    """

    if value is None:
        return None
    if isinstance(value, Command):
        command = cast(Command[object], value)
        raw_update = command.update
        if not isinstance(raw_update, Mapping):
            return command
        update = {
            **cast(Mapping[str, object], raw_update),
            **_lineage_update_for_role(
                cast(Mapping[str, object], raw_update),
                role=role,
                required=required,
            ),
        }
        updated_command: Command[object] = Command(
            graph=command.graph,
            update=update,
            resume=command.resume,
            goto=command.goto,
        )
        return updated_command
    if isinstance(value, Mapping):
        state = cast(Mapping[str, object], value)
        return {
            **state,
            **_lineage_update_for_role(state, role=role, required=required),
        }
    raise TypeError("Plan Graph input must be a mapping, Command, or None")


async def _checkpoint_state(
    planning: PlanningWorkflowGraph[Any],
    config: RunnableConfig,
) -> dict[str, object]:
    return await _plan_checkpoint_channels(planning.checkpointer, config)


async def _plan_checkpoint_channels(
    checkpointer: object,
    config: RunnableConfig,
) -> dict[str, object]:
    """Read the canonical Planning head independently of native checkpoint selection."""

    if not isinstance(checkpointer, BaseCheckpointSaver):
        return {}
    saver = cast(_CheckpointSaver, checkpointer)
    planning_config = cast(RunnableConfig, dict(config))
    configurable = dict(planning_config.get("configurable", {}))
    configurable.pop("checkpoint_id", None)
    planning_config["configurable"] = configurable
    async for checkpoint in saver.alist(
        planning_config,
        filter={"run_id": PLAN_CHECKPOINT_RUN_ID},
        limit=1,
    ):
        channels = checkpoint.checkpoint.get("channel_values")
        if not isinstance(channels, Mapping):
            return {}
        return dict(cast(Mapping[str, object], channels))
    return {}


async def _native_plan_overlay(
    native: _GraphRuntime,
    config: RunnableConfig,
    content: PlanContentBinding,
) -> PlanState[PlanContentModel] | None:
    state = await _plan_checkpoint_channels(native.checkpointer, config)
    if PLAN_STATE_KEY not in state:
        return None
    plan = read_plan_state(state, content)
    if plan.status is PlanStatus.APPROVED:
        handoff = plan.handoff
        if handoff is None or handoff.phase is PlanHandoffPhase.PENDING:
            raise PlanStateConflictError(
                "approved Plan has no native checkpoint evidence"
            )
        return plan
    return plan if plan.status is PlanStatus.CANCELLED else None


def _handoff_text(
    plan: PlanState[PlanContentModel],
    content_binding: PlanContentBinding,
) -> str:
    """Render the deterministic, digest-bound instruction appended to user input.

    The marker lets retries recognize the exact approved handoff. Content is rendered
    according to the frozen media type and explicitly preserves later Tool-specific
    review instead of treating Plan approval as blanket execution permission.
    """

    confirmed = plan.confirmed_plan
    handoff = plan.handoff
    if confirmed is None or handoff is None:
        raise RuntimeError("approved Plan state requires a confirmed handoff")
    if confirmed.content_schema != content_binding.reference:
        raise RuntimeError("approved Plan content schema does not match the Definition")
    content = confirmed.content
    if confirmed.content_schema.media_type == "text/markdown":
        if not isinstance(content, MarkdownPlanContent):
            raise RuntimeError("Markdown Plan handoff requires MarkdownPlanContent")
        rendered = content.markdown
    else:
        rendered = json.dumps(
            content.model_dump(mode="json", by_alias=True, exclude_none=False),
            ensure_ascii=False,
            indent=2,
        )
    return (
        f'{_APPROVED_PLAN_MARKER} digest="{handoff.digest}" '
        f'content-type="{confirmed.content_schema.media_type}" '
        f'revision="{confirmed.revision}">\n'
        "Plan review is complete and approval has already been granted. Begin native "
        "Deep Agent execution now. Do not restate the Plan or request general Plan "
        "approval again. Tool-specific human review remains mandatory. Treat this "
        "approved Plan as the governing task contract and do not silently change its "
        "content.\n\n" + rendered + "\n</tinkerfin-approved-plan>"
    )


def _content_contains_handoff(content: object, digest: str) -> bool:
    marker = f'{_APPROVED_PLAN_MARKER} digest="{digest}"'
    if isinstance(content, str):
        return marker in content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for block in cast(Sequence[object], content):
            if isinstance(block, str) and marker in block:
                return True
            if isinstance(block, Mapping):
                mapping = cast(Mapping[object, object], block)
                if marker in str(mapping.get("text", "")):
                    return True
    return False


def _handoff_message(
    state: Mapping[str, object],
    plan: PlanState[PlanContentModel],
    content_binding: PlanContentBinding,
) -> HumanMessage | None:
    """Append the approved Plan to exactly one checkpointed user message.

    Message ID, digest marker, and original content shape make the operation idempotent.
    Missing or duplicate targets fail before native state mutation.
    """

    handoff = plan.handoff
    if handoff is None:
        raise RuntimeError("approved Plan state requires handoff metadata")
    raw_messages = state.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise TypeError("Plan handoff requires checkpoint messages")
    matches = [
        message
        for message in cast(Sequence[object], raw_messages)
        if isinstance(message, HumanMessage) and message.id == handoff.message_id
    ]
    if len(matches) != 1:
        raise RuntimeError("Plan handoff message is missing or duplicated")
    message = matches[0]
    if _content_contains_handoff(message.content, handoff.digest):
        return None
    instruction = _handoff_text(plan, content_binding)
    if isinstance(message.content, str):
        content: object = f"{message.content}\n\n{instruction}"
    elif isinstance(message.content, list):
        content = [*message.content, {"type": "text", "text": instruction}]
    else:
        raise TypeError("Plan handoff supports string or content-block user messages")
    return message.model_copy(update={"content": content})


def _overlay_plan(
    part: Mapping[str, object],
    plan: PlanState[PlanContentModel],
) -> Mapping[str, object]:
    if part.get("type") != "values" or part.get("ns") != ():
        return part
    data = part.get("data")
    if not isinstance(data, Mapping):
        raise TypeError("root values data must be a mapping")
    updated = dict(part)
    updated["data"] = {
        **cast(Mapping[str, object], data),
        PLAN_STATE_KEY: plan.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
    }
    return updated


def _checkpoint_id(snapshot: StateSnapshot) -> str:
    value = snapshot.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(value, str) or not value:
        raise PlanStateConflictError("native checkpoint has no stable checkpoint_id")
    return value


def _snapshot_handoff_message(
    snapshot: StateSnapshot,
    plan: PlanState[PlanContentModel],
    *,
    allow_unmarked_original: bool = False,
) -> HumanMessage | None:
    """Verify whether a native checkpoint contains the exact approved handoff.

    ``allow_unmarked_original`` is limited to the pre-update checkpoint used during the
    atomic handoff transition; every later checkpoint must retain the digest marker.
    """

    handoff = plan.handoff
    if handoff is None:
        raise PlanStateConflictError("approved Plan has no handoff metadata")
    raw_messages = snapshot.values.get("messages")
    if raw_messages is None:
        return None
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise PlanStateConflictError("native checkpoint messages are not a sequence")
    matches = [
        message
        for message in cast(Sequence[object], raw_messages)
        if isinstance(message, HumanMessage) and message.id == handoff.message_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise PlanStateConflictError("native checkpoint duplicates the Plan handoff")
    message = matches[0]
    if not _content_contains_handoff(message.content, handoff.digest):
        if allow_unmarked_original:
            return None
        raise PlanStateConflictError("native checkpoint handoff content conflicts")
    return message


def _is_resume_command(value: object) -> bool:
    return (
        isinstance(value, Command) and cast(Command[object], value).resume is not None
    )


def _plan_values_part(
    plan: PlanState[PlanContentModel],
    *,
    native_values: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    return {
        "type": "values",
        "ns": (),
        "data": {
            **({} if native_values is None else native_values),
            PLAN_STATE_KEY: plan.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
        },
        "interrupts": (),
    }


class PlanCapableGraphRuntime:
    """Route one request without wrapping or modifying the native Deep Agent graph.

    Ordinary default inputs and every Tool resume delegate directly to the native
    graph. Plan inputs and Plan resumes use the standalone Planning graph. An approved
    Planning run is synchronously handed to the same native graph in this request.
    The wrapper owns no external resource; both graphs borrow the Definition's saver,
    Store, cache, backend, and context.
    """

    __slots__ = (
        "_content",
        "_native",
        "_options",
        "_planning_factory",
        "_prefer_plan",
        "_signature",
    )

    def __init__(
        self,
        *,
        options: PlanOptions,
        native: _GraphRuntime,
        planning_factory: Callable[[], PlanningWorkflowGraph[Any]],
        prefer_plan: bool,
    ) -> None:
        """Bind borrowed native and lazily created Planning graphs to one router."""

        self._options = options
        self._content = options.content
        self._native = native
        self._planning_factory = planning_factory
        self._prefer_plan = prefer_plan
        self._signature = inspect.signature(native.astream)

    @property
    def checkpointer(self) -> object:
        """Expose the native graph checkpointer without taking ownership."""

        return self._native.checkpointer

    async def _tinkerfin_lineage_state(
        self,
        config: RunnableConfig,
        role: str,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot:
        """Read an exact native or Planning checkpoint for lineage validation."""

        if role == NATIVE_CHECKPOINT_ROLE:
            return await self._native.aget_state(config, subgraphs=subgraphs)
        if role == PLANNING_CHECKPOINT_ROLE:
            return await self._planning_factory().aget_state(
                config,
                subgraphs=subgraphs,
            )
        raise PlanStateConflictError("checkpoint lineage has an unknown graph role")

    def astream(
        self, *args: object, **kwargs: object
    ) -> AsyncIterator[Mapping[str, object]]:
        """Return the single request stream selected by input and pending origin."""

        return self._stream(*args, **kwargs)

    async def _stream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        bound = self._signature.bind(*args, **kwargs)
        graph_input = bound.arguments.get("input")
        config = _config(bound)
        configurable = config.get("configurable", {})
        lineage_required = RUN_ID_METADATA_KEY in configurable
        runtime_profile = configurable.get(RUNTIME_PROFILE_METADATA_KEY)
        if lineage_required and (
            not isinstance(runtime_profile, str) or not runtime_profile
        ):
            raise PlanStateConflictError(
                "lineage routing requires a canonical Runtime Profile"
            )
        if (
            not self._prefer_plan
            and not _is_resume_command(graph_input)
            and not isinstance(self._native.checkpointer, BaseCheckpointSaver)
        ):
            async for part in self._native.astream(*bound.args, **bound.kwargs):
                yield part
            return
        planning = self._planning_factory()
        checkpoint_state = await _checkpoint_state(planning, config)
        checkpoint_plan = (
            read_plan_state(checkpoint_state, self._content)
            if PLAN_STATE_KEY in checkpoint_state
            else None
        )

        use_planning = self._prefer_plan
        if _is_resume_command(graph_input):
            planning_snapshot: StateSnapshot | None = None
            if lineage_required:
                thread_id = configurable.get("thread_id")
                if not isinstance(thread_id, str):
                    raise PlanStateConflictError(
                        "resume routing requires a canonical AG-UI thread ID"
                    )
                head = await resolve_agui_thread_head(
                    self.checkpointer,
                    thread_id=thread_id,
                    runtime_profile=cast(str, runtime_profile),
                )
                if head is None:
                    raise PlanStateConflictError(
                        "resume input has no canonical thread checkpoint"
                    )
                planning_pending = False
                native_pending = False
                if head.role == PLANNING_CHECKPOINT_ROLE:
                    planning_snapshot = await planning.aget_state(head.config)
                    planning_values = cast(
                        Mapping[str, object], planning_snapshot.values
                    )
                    checkpoint_state = dict(planning_values)
                    checkpoint_plan = (
                        read_plan_state(checkpoint_state, self._content)
                        if PLAN_STATE_KEY in checkpoint_state
                        else None
                    )
                    planning_pending = bool(planning_snapshot.next)
                    if _native_checkpoint_id_for_plan(checkpoint_plan) is not None:
                        (
                            native_snapshot,
                            _native_config,
                        ) = await _native_snapshot_for_plan(
                            self._native,
                            config,
                            checkpoint_plan,
                        )
                        native_pending = bool(native_snapshot.next)
                else:
                    native_snapshot = await self._native.aget_state(head.config)
                    native_pending = bool(native_snapshot.next)
            else:
                planning_snapshot = await planning.aget_state(config)
                native_snapshot, _native_config = await _native_snapshot_for_plan(
                    self._native,
                    config,
                    checkpoint_plan,
                )
                planning_pending = bool(planning_snapshot.next)
                native_pending = bool(native_snapshot.next)
            if planning_pending and native_pending:
                raise PlanStateConflictError(
                    "Planning and native Graphs both contain pending work"
                )
            if planning_pending:
                if planning_snapshot is None or PLAN_STATE_KEY not in checkpoint_state:
                    raise PlanStateConflictError(
                        "pending Planning checkpoint has no Plan state"
                    )
                checkpoint_plan = read_plan_state(checkpoint_state, self._content)
                if checkpoint_plan.status is PlanStatus.AWAITING_CLARIFICATION:
                    if (
                        checkpoint_state.get(PLAN_SCHEMA_FINGERPRINT_KEY)
                        != self._options.clarification.fingerprint
                    ):
                        raise PlanModeConfigurationError(
                            "checkpoint clarification schema does not match this Definition"
                        )
                    pending = checkpoint_plan.pending_clarification
                    if pending is None:
                        raise PlanStateConflictError(
                            "clarification resume has no pending form"
                        )
                    if pending.contract_digest != pending_contract_digest(
                        pending.form,
                        pending.response_schema,
                    ):
                        raise PlanStateConflictError(
                            "clarification resume has an invalid contract digest"
                        )
                    command = cast(Command[object], graph_input)
                    form = restore_form(self._options.clarification, pending.form)
                    validate_and_normalize_response(
                        self._options.clarification,
                        form,
                        pending.response_schema,
                        command.resume,
                    )
                use_planning = True
            elif native_pending:
                use_planning = bool(
                    checkpoint_plan is not None
                    and checkpoint_plan.status is PlanStatus.APPROVED
                    and checkpoint_plan.handoff is not None
                    and checkpoint_plan.handoff.phase is PlanHandoffPhase.PENDING
                )
            elif checkpoint_plan is not None and checkpoint_plan.status in {
                PlanStatus.APPROVED,
                PlanStatus.CANCELLED,
            }:
                yield _plan_values_part(checkpoint_plan)
                return
            else:
                raise PlanStateConflictError(
                    "resume input has no pending Planning or native checkpoint"
                )

        if not use_planning:
            overlay = await _native_plan_overlay(
                self._native,
                config,
                self._content,
            )
            native_interrupted = False
            async for part in self._native.astream(*bound.args, **bound.kwargs):
                if part.get("type") == "values" and part.get("ns") == ():
                    native_interrupted = bool(part.get("interrupts", ()))
                yield part if overlay is None else _overlay_plan(part, overlay)
            if (
                overlay is not None
                and overlay.handoff is not None
                and overlay.handoff.phase is PlanHandoffPhase.ACCEPTED
            ):
                snapshot = await self._native.aget_state(config)
                if not native_interrupted:
                    overlay = await planning.mark_handoff_phase(
                        config,
                        overlay,
                        phase=PlanHandoffPhase.COMPLETED,
                        native_checkpoint_id=overlay.handoff.native_checkpoint_id
                        or _checkpoint_id(snapshot),
                        completed_checkpoint_id=_checkpoint_id(snapshot),
                    )
                    yield _plan_values_part(
                        overlay,
                        native_values=cast(Mapping[str, object], snapshot.values),
                    )
            return

        final_state: Mapping[str, object] = checkpoint_state
        final_plan = checkpoint_plan
        if (
            not isinstance(graph_input, Command)
            or checkpoint_plan is None
            or checkpoint_plan.status not in {PlanStatus.APPROVED, PlanStatus.CANCELLED}
        ):
            interrupted = False
            planning_bound = self._signature.bind(*bound.args, **bound.kwargs)
            planning_bound.arguments["input"] = _input_with_lineage_role(
                cast(object, graph_input),
                role="planning",
                required=lineage_required,
            )
            async for part in planning.astream(
                *planning_bound.args,
                **planning_bound.kwargs,
            ):
                if part.get("type") == "values" and part.get("ns") == ():
                    data = part.get("data")
                    if not isinstance(data, Mapping):
                        raise TypeError("Planning root values data must be a mapping")
                    final_state = cast(Mapping[str, object], data)
                    final_plan = read_plan_state(final_state, self._content)
                    interrupted = bool(part.get("interrupts", ()))
                yield part
            if interrupted:
                return

        if final_plan is None or final_plan.status is PlanStatus.CANCELLED:
            return
        if final_plan.status is not PlanStatus.APPROVED:
            raise RuntimeError(
                "Planning ended without an interrupt or approved handoff"
            )
        if final_plan.handoff is None:
            raise RuntimeError("approved Plan state requires handoff metadata")
        native_snapshot, native_config = await _native_snapshot_for_plan(
            self._native,
            config,
            final_plan,
        )
        existing_message = _snapshot_handoff_message(
            native_snapshot,
            final_plan,
            allow_unmarked_original=(
                final_plan.handoff.phase is PlanHandoffPhase.PENDING
            ),
        )
        accepted_this_call = False
        if final_plan.handoff.phase is PlanHandoffPhase.PENDING:
            if existing_message is None:
                if native_snapshot.next:
                    raise PlanStateConflictError(
                        "native Graph has pending work before Plan handoff"
                    )
                message = _handoff_message(final_state, final_plan, self._content)
                if message is None:
                    raise PlanStateConflictError(
                        "Planning checkpoint already contains a native handoff marker"
                    )
                native_config = await self._native.aupdate_state(
                    config,
                    {
                        "messages": [message],
                        **_lineage_update_for_role(
                            final_state,
                            role="native",
                            required=lineage_required,
                        ),
                    },
                    as_node=START,
                )
                native_checkpoint_id = cast(
                    str,
                    native_config.get("configurable", {}).get("checkpoint_id"),
                )
                if not native_checkpoint_id:
                    raise PlanStateConflictError(
                        "native handoff update returned no checkpoint_id"
                    )
                native_snapshot = await self._native.aget_state(native_config)
            else:
                native_checkpoint_id = _checkpoint_id(native_snapshot)
                native_config = _select_checkpoint(config, native_checkpoint_id)
            final_plan = await planning.mark_handoff_phase(
                config,
                final_plan,
                phase=PlanHandoffPhase.ACCEPTED,
                native_checkpoint_id=native_checkpoint_id,
            )
            accepted_this_call = True
        elif existing_message is None:
            raise PlanStateConflictError(
                "accepted Plan handoff is missing from the native checkpoint"
            )

        handoff = final_plan.handoff
        if handoff is None:
            raise PlanStateConflictError("approved Plan lost handoff metadata")
        if handoff.phase is PlanHandoffPhase.COMPLETED:
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )
            return
        if not native_snapshot.next and not accepted_this_call:
            final_plan = await planning.mark_handoff_phase(
                config,
                final_plan,
                phase=PlanHandoffPhase.COMPLETED,
                native_checkpoint_id=handoff.native_checkpoint_id
                or _checkpoint_id(native_snapshot),
                completed_checkpoint_id=_checkpoint_id(native_snapshot),
            )
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )
            return
        if native_snapshot.interrupts:
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )
            return

        native_bound = self._signature.bind(*bound.args, **bound.kwargs)
        native_bound.arguments["input"] = None
        native_bound.arguments["config"] = native_config
        durability = native_bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise PlanModeConfigurationError("Plan handoff requires durability='sync'")
        native_bound.arguments["durability"] = "sync"
        native_interrupted = False
        async for part in self._native.astream(
            *native_bound.args,
            **native_bound.kwargs,
        ):
            if part.get("type") == "values" and part.get("ns") == ():
                native_interrupted = bool(part.get("interrupts", ()))
            yield _overlay_plan(part, final_plan)
        if lineage_required:
            thread_id = configurable.get("thread_id")
            semantic_run_id = configurable.get(RUN_ID_METADATA_KEY)
            if not isinstance(thread_id, str) or not isinstance(semantic_run_id, str):
                raise PlanStateConflictError(
                    "native completion requires canonical AG-UI lineage identifiers"
                )
            native_head = await resolve_agui_native_run_head(
                self.checkpointer,
                thread_id=thread_id,
                run_id=semantic_run_id,
                runtime_profile=cast(str, runtime_profile),
            )
            native_snapshot = await self._native.aget_state(native_head.config)
        else:
            native_snapshot = await self._native.aget_state(config)
        if not native_interrupted:
            handoff = final_plan.handoff
            if handoff is None:
                raise PlanStateConflictError("approved Plan lost handoff metadata")
            final_plan = await planning.mark_handoff_phase(
                config,
                final_plan,
                phase=PlanHandoffPhase.COMPLETED,
                native_checkpoint_id=handoff.native_checkpoint_id
                or _checkpoint_id(native_snapshot),
                completed_checkpoint_id=_checkpoint_id(native_snapshot),
            )
            yield _plan_values_part(
                final_plan,
                native_values=cast(Mapping[str, object], native_snapshot.values),
            )


setattr(
    PlanCapableGraphRuntime.astream,
    "__signature__",
    inspect.signature(_COMPILED_ASTREAM),
)


__all__ = ["PlanCapableGraphRuntime"]
