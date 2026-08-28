"""Checkpoint lineage owned by the public AG-UI run boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.types import StateSnapshot
from pydantic import ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_native_stream import NativeRuntimeInterrupt

from ._agui_lineage_state import (
    CHECKPOINT_ROLE_METADATA_KEY,
    LINEAGE_STATE_KEY,
    NATIVE_CHECKPOINT_ROLE,
    PARENT_RUN_ID_METADATA_KEY,
    PLANNING_CHECKPOINT_ROLE,
    PLANNING_CHECKPOINT_RUN_ID,
    RESUME_MARKER_STATE_KEY,
    RUN_ID_METADATA_KEY,
    RUNTIME_PROFILE_METADATA_KEY,
    LineageMarker,
    LineageRole,
    lineage_state_update,
    parse_lineage_marker,
)
from ._tasks import join_task
from .errors import TinkerFinLifecycleError

if TYPE_CHECKING:
    from .agui_resume import AgUiResumeBinding, _ResumeMarker
    from .runtime_profile import DeepAgentsRuntimeProfile

_CHECKPOINTER_RUN_ID_KEY = "run_id"
_MAX_RESUME_ANCESTRY_DEPTH = 4096

_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


@dataclass(frozen=True, slots=True)
class AgUiLineageResolution:
    """Resolved invocation checkpoint and durable resume progress."""

    config: RunnableConfig
    resume_phase: Literal["none", "unstaged", "prepared", "accepted"]
    parent_run_id: str | None
    checkpoint_role: LineageRole


@dataclass(frozen=True, slots=True)
class _DurableResumeProgress:
    """Verified staged intent, its source checkpoint, and submission progress."""

    head: CheckpointTuple
    stage: CheckpointTuple
    source: CheckpointTuple
    marker: _ResumeMarker
    phase: Literal["prepared", "accepted"]


@dataclass(frozen=True, slots=True)
class AgUiThreadHead:
    """Saver-neutral canonical thread head and its owning Graph role."""

    config: RunnableConfig
    role: LineageRole


@dataclass(frozen=True, slots=True)
class AgUiResumeContext:
    """Hold trusted pending interrupts and message correlation from one checkpoint."""

    interrupts: tuple[NativeRuntimeInterrupt, ...]
    messages_by_namespace: Mapping[tuple[str, ...], tuple[BaseMessage, ...]]


def _checkpoint_id(checkpoint: CheckpointTuple) -> str:
    value = checkpoint.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(value, str) or not value:
        raise TinkerFinLifecycleError("AG-UI lineage checkpoint has no stable ID")
    return value


def _checkpoint_ancestry_key(checkpoint: CheckpointTuple) -> tuple[str, str, str]:
    """Return the complete saver location used to detect corrupt parent cycles."""

    configurable = checkpoint.config.get("configurable", {})
    thread_id = configurable.get("thread_id")
    namespace = configurable.get("checkpoint_ns", "")
    if not isinstance(thread_id, str) or not thread_id:
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no stable thread identity"
        )
    if not isinstance(namespace, str):
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no stable namespace"
        )
    return thread_id, namespace, _checkpoint_id(checkpoint)


def _checkpoint_role(checkpoint: CheckpointTuple) -> str:
    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no private lineage marker"
        )
    return marker.role


def _require_runtime_profile(
    checkpoint: CheckpointTuple,
    *,
    expected: str,
) -> None:
    """Reject a checkpoint created by another explicit Runtime Profile."""

    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError(
            "checkpoint has no private Runtime Profile marker"
        )
    if marker.runtime_profile != expected:
        raise TinkerFinLifecycleError(
            "checkpoint belongs to another Runtime Profile",
            context={"runtime_profile": expected},
            diagnostic_context={"checkpoint_runtime_profile": marker.runtime_profile},
        )


def _checkpoint_lineage(checkpoint: CheckpointTuple) -> LineageMarker | None:
    """Resolve checkpoint ownership from LangGraph checkpoint source semantics.

    ``source="input"`` checkpoints still commit the source state while their pending
    writes contain the new invocation input. Other checkpoint sources commit their
    own state, and their pending writes belong to the next super-step. This follows
    ``langgraph.checkpoint.base.CheckpointMetadata.source`` and prevents child Graph
    writes from retrospectively changing a committed parent's lineage.
    """

    channel_values = checkpoint.checkpoint.get("channel_values")
    committed = (
        cast(Mapping[object, object], channel_values).get(LINEAGE_STATE_KEY)
        if isinstance(channel_values, Mapping)
        else None
    )
    pending = [
        value
        for _task_id, channel, value in checkpoint.pending_writes or ()
        if channel == LINEAGE_STATE_KEY
    ]
    raw_values = (
        pending
        if checkpoint.metadata.get("source") == "input" and pending
        else ([] if committed is None else [committed])
    )
    markers: list[LineageMarker] = []
    for value in raw_values:
        try:
            marker = parse_lineage_marker(value)
        except ValidationError as error:
            raise TinkerFinLifecycleError(
                "checkpoint contains an invalid private lineage marker"
            ) from error
        if marker not in markers:
            markers.append(marker)
    if len(markers) > 1:
        raise TinkerFinLifecycleError(
            "checkpoint contains conflicting private lineage markers"
        )
    return markers[0] if markers else None


async def _run_checkpoints(
    checkpointer: _CheckpointSaver,
    *,
    thread_id: str,
    run_id: str,
) -> list[CheckpointTuple]:
    """List root checkpoints created by one run from newest to oldest."""

    candidates: dict[str, CheckpointTuple] = {}
    for indexed_run_id in (run_id, PLANNING_CHECKPOINT_RUN_ID):
        config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                _CHECKPOINTER_RUN_ID_KEY: indexed_run_id,
            }
        }
        async for checkpoint in checkpointer.alist(config):
            marker = _checkpoint_lineage(checkpoint)
            if marker is None:
                continue
            if marker.thread_id != thread_id:
                raise TinkerFinLifecycleError(
                    "checkpoint lineage marker belongs to a different thread"
                )
            if marker.run_id != run_id:
                continue
            candidates[_checkpoint_id(checkpoint)] = checkpoint
    return list(candidates.values())


def _unique_leaf(
    checkpoints: list[CheckpointTuple],
    *,
    missing_message: str,
    ambiguous_message: str,
    run_id: str,
) -> CheckpointTuple:
    """Select one durable leaf without trusting list order or saver internals.

    Parent checkpoint references define the local DAG. Zero leaves means missing
    evidence and multiple leaves mean ambiguous lineage; neither case may silently pick
    the newest row because branch and resume must be deterministic across saver types.
    """

    if not checkpoints:
        raise TinkerFinLifecycleError(
            missing_message,
            context={"parent_run_id": run_id},
        )

    checkpoint_ids = {_checkpoint_id(checkpoint) for checkpoint in checkpoints}
    referenced_parents = {
        parent_id
        for checkpoint in checkpoints
        if checkpoint.parent_config is not None
        for parent_id in (
            checkpoint.parent_config.get("configurable", {}).get("checkpoint_id"),
        )
        if isinstance(parent_id, str) and parent_id in checkpoint_ids
    }
    heads = [
        checkpoint
        for checkpoint in checkpoints
        if _checkpoint_id(checkpoint) not in referenced_parents
    ]
    if len(heads) != 1:
        raise TinkerFinLifecycleError(
            ambiguous_message,
            context={"parent_run_id": run_id},
        )
    return heads[0]


def _completed_plan_native_leaf(
    checkpoints: list[CheckpointTuple],
    *,
    run_id: str,
) -> CheckpointTuple | None:
    """Resolve a completed Planning branch to its durable native terminal."""

    completed_ids: set[str] = set()
    for checkpoint in checkpoints:
        marker = _checkpoint_lineage(checkpoint)
        if marker is None or marker.role != PLANNING_CHECKPOINT_ROLE:
            continue
        channel_values = checkpoint.checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            continue
        plan = cast(Mapping[object, object], channel_values).get("tinkerfin_plan")
        if not isinstance(plan, Mapping):
            continue
        handoff = cast(Mapping[object, object], plan).get("handoff")
        if not isinstance(handoff, Mapping):
            continue
        handoff_values = cast(Mapping[object, object], handoff)
        if handoff_values.get("phase") != "completed":
            continue
        completed_id = handoff_values.get(
            "completedCheckpointId",
            handoff_values.get("completed_checkpoint_id"),
        )
        if not isinstance(completed_id, str) or not completed_id:
            raise TinkerFinLifecycleError(
                "completed Plan lineage has no native terminal checkpoint",
                context={"parent_run_id": run_id},
            )
        completed_ids.add(completed_id)
    if not completed_ids:
        return None
    if len(completed_ids) != 1:
        raise TinkerFinLifecycleError(
            "completed Plan lineage references ambiguous native terminals",
            context={"parent_run_id": run_id},
        )
    completed_id = next(iter(completed_ids))
    matches = [
        checkpoint
        for checkpoint in checkpoints
        if _checkpoint_id(checkpoint) == completed_id
        and (marker := _checkpoint_lineage(checkpoint)) is not None
        and marker.role == NATIVE_CHECKPOINT_ROLE
    ]
    if len(matches) != 1:
        raise TinkerFinLifecycleError(
            "completed Plan lineage cannot resolve its native terminal",
            context={"parent_run_id": run_id},
        )
    return matches[0]


async def _run_head(
    checkpointer: _CheckpointSaver,
    *,
    thread_id: str,
    run_id: str,
) -> CheckpointTuple:
    """Return the unique root checkpoint leaf created by one run."""

    checkpoints = await _run_checkpoints(
        checkpointer,
        thread_id=thread_id,
        run_id=run_id,
    )
    completed_plan = _completed_plan_native_leaf(checkpoints, run_id=run_id)
    if completed_plan is not None:
        return completed_plan
    return _unique_leaf(
        checkpoints,
        missing_message="parentRunId has no checkpoint in this canonical thread",
        ambiguous_message="parentRunId resolves to ambiguous checkpoint branches",
        run_id=run_id,
    )


async def resolve_agui_native_run_head(
    checkpointer: object,
    *,
    thread_id: str,
    run_id: str,
    runtime_profile: str,
) -> CheckpointTuple:
    """Return the unique native checkpoint leaf for one indexed semantic run."""

    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "native run lineage requires a concrete BaseCheckpointSaver"
        )
    checkpoints = await _run_checkpoints(
        cast(_CheckpointSaver, checkpointer),
        thread_id=thread_id,
        run_id=run_id,
    )
    native = [
        checkpoint
        for checkpoint in checkpoints
        if (marker := _checkpoint_lineage(checkpoint)) is not None
        and marker.role == NATIVE_CHECKPOINT_ROLE
    ]
    selected = _unique_leaf(
        native,
        missing_message="native run has no checkpoint in this canonical thread",
        ambiguous_message="native run resolves to ambiguous checkpoint branches",
        run_id=run_id,
    )
    _require_runtime_profile(selected, expected=runtime_profile)
    return selected


async def _read_snapshot(
    astream: Callable[..., object],
    checkpoint: CheckpointTuple,
) -> StateSnapshot:
    owner = getattr(astream, "__self__", None)
    role = _checkpoint_role(checkpoint)
    role_reader = getattr(owner, "_tinkerfin_lineage_state", None)
    if callable(role_reader):
        return await cast(Callable[..., Awaitable[StateSnapshot]], role_reader)(
            checkpoint.config,
            role,
            subgraphs=True,
        )
    if role != NATIVE_CHECKPOINT_ROLE:
        raise TinkerFinLifecycleError(
            "planning checkpoint requires a Plan-capable lineage reader"
        )
    reader = getattr(owner, "aget_state", None)
    if not callable(reader):
        raise TinkerFinLifecycleError(
            "parentRunId requires a graph with asynchronous state inspection"
        )
    return await cast(Callable[..., Awaitable[StateSnapshot]], reader)(
        checkpoint.config,
        subgraphs=True,
    )


def _interrupt_ids(snapshot: StateSnapshot) -> frozenset[str]:
    ids = frozenset(interrupt.id for interrupt in snapshot.interrupts)
    if len(ids) != len(snapshot.interrupts):
        raise TinkerFinLifecycleError(
            "parent checkpoint contains duplicate interrupt identities"
        )
    return ids


def _snapshot_namespace(snapshot: StateSnapshot) -> tuple[str, ...]:
    """Decode LangGraph's complete checkpoint namespace without truncating IDs."""

    value = snapshot.config.get("configurable", {}).get("checkpoint_ns", "")
    if not isinstance(value, str):
        raise TinkerFinLifecycleError("checkpoint namespace is not text")
    if not value:
        return ()
    components = tuple(value.split("|"))
    if any(not component for component in components):
        raise TinkerFinLifecycleError("checkpoint namespace has an empty component")
    return components


def _resume_context(snapshot: StateSnapshot) -> AgUiResumeContext:
    """Collect root/subgraph messages and pending interrupts from one state tree."""

    messages_by_namespace: dict[tuple[str, ...], tuple[BaseMessage, ...]] = {}
    interrupts_by_id: dict[str, NativeRuntimeInterrupt] = {}

    def visit(current: StateSnapshot) -> None:
        namespace = _snapshot_namespace(current)
        values = current.values
        if not isinstance(values, Mapping):
            raise TinkerFinLifecycleError("checkpoint values are not a mapping")
        raw_messages = cast(Mapping[object, object], values).get("messages", ())
        if not isinstance(raw_messages, (list, tuple)):
            raise TinkerFinLifecycleError("checkpoint messages are not a sequence")
        message_values = cast(list[object] | tuple[object, ...], raw_messages)
        resolved_messages = tuple(
            message for message in message_values if isinstance(message, BaseMessage)
        )
        if len(resolved_messages) != len(message_values):
            raise TinkerFinLifecycleError(
                "checkpoint messages contain a non-LangChain value"
            )
        previous_messages = messages_by_namespace.get(namespace)
        if previous_messages is not None and previous_messages != resolved_messages:
            raise TinkerFinLifecycleError(
                "checkpoint repeats one namespace with conflicting messages"
            )
        messages_by_namespace[namespace] = resolved_messages
        for raw_interrupt in current.interrupts:
            interrupt = NativeRuntimeInterrupt.model_validate(raw_interrupt)
            previous_interrupt = interrupts_by_id.get(interrupt.id)
            if previous_interrupt is not None and previous_interrupt != interrupt:
                raise TinkerFinLifecycleError(
                    "checkpoint repeats one interrupt ID with conflicting values"
                )
            interrupts_by_id[interrupt.id] = interrupt
        for task in current.tasks:
            if isinstance(task.state, StateSnapshot):
                visit(task.state)

    visit(snapshot)
    if not interrupts_by_id:
        raise TinkerFinLifecycleError("checkpoint has no pending interrupt to resume")
    return AgUiResumeContext(
        interrupts=tuple(interrupts_by_id.values()),
        messages_by_namespace=MappingProxyType(dict(messages_by_namespace)),
    )


async def resolve_agui_resume_context(
    astream: Callable[..., object],
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> AgUiResumeContext:
    """Resolve trusted resume correlation from the Profile-bound canonical head.

    Runtime Profile ownership is verified before Graph state is loaded. The returned
    context comes only from the configured checkpointer; client and host databases do
    not supply interrupt payloads or Tool correlation.

    Args:
        astream: Profile-bound Graph stream exposing the concrete checkpointer.
        identity: New resume Run identity in the existing checkpoint thread.
        parent_run_id: Optional interrupted Run selected explicitly by the host.
        runtime_profile: Exact Profile required by checkpoint lineage.

    Returns:
        Trusted pending interrupts and message correlation from the canonical source.

    Raises:
        TinkerFinLifecycleError: Lineage, Profile, checkpoint, or interrupt evidence is
            missing, ambiguous, or inconsistent.
    """

    checkpointer = _require_checkpointer(astream)
    if parent_run_id is None:
        head = await _thread_head(checkpointer, thread_id=identity.thread_id)
        if head is None:
            raise TinkerFinLifecycleError(
                "resume has no checkpoint in this canonical thread"
            )
        checkpoint = (
            await _resume_source_from_head(
                checkpointer,
                head=head,
                identity=identity,
                runtime_profile=runtime_profile,
            )
            or head
        )
    else:
        checkpoint = await _run_head(
            checkpointer,
            thread_id=identity.thread_id,
            run_id=parent_run_id,
        )
    _require_runtime_profile(checkpoint, expected=runtime_profile.profile_id)
    snapshot = await _read_snapshot(astream, checkpoint)
    failures = tuple(task.error for task in snapshot.tasks if task.error is not None)
    if failures:
        raise TinkerFinLifecycleError(
            "resume checkpoint contains failed task evidence",
            diagnostic_context={"failed_task_count": len(failures)},
        )
    return _resume_context(snapshot)


async def _thread_head(
    checkpointer: _CheckpointSaver,
    *,
    thread_id: str,
) -> CheckpointTuple | None:
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    return await checkpointer.aget_tuple(config)


async def resolve_agui_thread_head(
    checkpointer: object,
    *,
    thread_id: str,
    runtime_profile: str,
) -> AgUiThreadHead | None:
    """Resolve the canonical thread head without asking the wrong Graph to load it.

    Reading ``CheckpointTuple`` directly preserves pending sends until the owning
    Graph is known. Calling ``aget_state()`` on another Graph can otherwise discard
    unknown node sends while merely trying to determine the resume route.

    Args:
        checkpointer: Concrete saver shared by the native and Planning Graphs.
        thread_id: Canonical AG-UI and checkpoint thread identifier.

    Returns:
        The exact head config and owning Graph role, or ``None`` for a new thread.

    Raises:
        TinkerFinLifecycleError: If the saver or private lineage marker is invalid.
    """

    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "AG-UI resume routing requires a concrete BaseCheckpointSaver"
        )
    checkpoint = await _thread_head(
        cast(_CheckpointSaver, checkpointer),
        thread_id=thread_id,
    )
    if checkpoint is None:
        return None
    _require_runtime_profile(checkpoint, expected=runtime_profile)
    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError("AG-UI thread head has no private lineage marker")
    if marker.thread_id != thread_id:
        raise TinkerFinLifecycleError(
            "AG-UI thread head lineage belongs to a different thread"
        )
    return AgUiThreadHead(config=checkpoint.config, role=marker.role)


def _committed_resume_marker_for_identity(
    checkpoint: CheckpointTuple,
    *,
    identity: RunIdentity,
) -> _ResumeMarker | None:
    """Return one committed marker owned by a semantic Run."""

    channel_values = checkpoint.checkpoint.get("channel_values")
    if not isinstance(channel_values, Mapping):
        return None
    raw_marker = cast(Mapping[object, object], channel_values).get(
        RESUME_MARKER_STATE_KEY
    )
    if raw_marker is None:
        return None
    from .agui_resume import parse_resume_marker

    marker = parse_resume_marker(raw_marker)
    if marker is None:
        raise TinkerFinLifecycleError(
            "checkpoint contains an invalid committed resume marker"
        )
    if marker.thread_id != identity.thread_id or marker.run_id != identity.run_id:
        return None
    return marker


def _resume_marker_for_identity(
    checkpoint: CheckpointTuple,
    *,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> _ResumeMarker | None:
    """Resolve one marker from committed state and Profile-owned pending writes."""

    from .agui_resume import parse_resume_marker

    raw_values: list[object] = []
    committed = _committed_resume_marker_for_identity(
        checkpoint,
        identity=identity,
    )
    if committed is not None:
        raw_values.append(committed)
    raw_values.extend(
        runtime_profile.pending_resume_values(
            checkpoint,
            channel_name=RESUME_MARKER_STATE_KEY,
        )
    )
    matches: list[_ResumeMarker] = []
    for raw_value in raw_values:
        marker = parse_resume_marker(raw_value)
        if marker is None:
            raise TinkerFinLifecycleError(
                "checkpoint contains an invalid pending resume marker"
            )
        if marker.thread_id != identity.thread_id or marker.run_id != identity.run_id:
            continue
        if marker not in matches:
            matches.append(marker)
    if len(matches) > 1:
        raise TinkerFinLifecycleError(
            "checkpoint contains conflicting durable markers for one runId",
            context={"run_id": identity.run_id},
        )
    return matches[0] if matches else None


def _pending_resume_lineage_ready(
    checkpoint: CheckpointTuple,
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> bool:
    """Validate the pending lineage half of one prepared resume intent."""

    raw_values = runtime_profile.pending_resume_values(
        checkpoint,
        channel_name=LINEAGE_STATE_KEY,
    )
    if not raw_values:
        return False
    markers: list[LineageMarker] = []
    for raw_value in raw_values:
        try:
            marker = parse_lineage_marker(raw_value)
        except ValidationError as error:
            raise TinkerFinLifecycleError(
                "checkpoint contains invalid pending resume lineage"
            ) from error
        if marker not in markers:
            markers.append(marker)
    expected_raw = lineage_state_update(
        identity=identity,
        parent_run_id=parent_run_id,
        runtime_profile=runtime_profile.profile_id,
        role=cast(LineageRole, _checkpoint_role(checkpoint)),
    )[LINEAGE_STATE_KEY]
    expected = parse_lineage_marker(expected_raw)
    if len(markers) != 1 or markers[0] != expected:
        raise TinkerFinLifecycleError(
            "checkpoint contains conflicting pending resume lineage",
            context={"run_id": identity.run_id},
        )
    return True


async def _checkpoint_parent(
    checkpointer: _CheckpointSaver,
    checkpoint: CheckpointTuple,
) -> CheckpointTuple:
    """Load the exact durable parent required by resume-intent ancestry."""

    parent_config = checkpoint.parent_config
    if parent_config is None:
        raise TinkerFinLifecycleError("durable resume intent has no source checkpoint")
    parent = await checkpointer.aget_tuple(parent_config)
    if parent is None:
        raise TinkerFinLifecycleError(
            "durable resume intent source checkpoint is unavailable"
        )
    return parent


async def _resume_stage_and_source(
    checkpointer: _CheckpointSaver,
    *,
    head: CheckpointTuple,
    identity: RunIdentity,
    marker: _ResumeMarker,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> tuple[CheckpointTuple, CheckpointTuple]:
    """Resolve the original pending-write stage and first committed descendant."""

    if _committed_resume_marker_for_identity(head, identity=identity) is None:
        pending = _resume_marker_for_identity(
            head,
            identity=identity,
            runtime_profile=runtime_profile,
        )
        if pending != marker:
            raise TinkerFinLifecycleError(
                "durable resume pending-write evidence is incomplete"
            )
        return head, head

    current = head
    visited: set[tuple[str, str, str]] = set()
    for _depth in range(_MAX_RESUME_ANCESTRY_DEPTH):
        ancestry_key = _checkpoint_ancestry_key(current)
        if ancestry_key in visited:
            raise TinkerFinLifecycleError(
                "durable resume ancestry contains a checkpoint cycle",
                context={"run_id": identity.run_id},
            )
        visited.add(ancestry_key)
        current_marker = _committed_resume_marker_for_identity(
            current,
            identity=identity,
        )
        if current_marker != marker:
            raise TinkerFinLifecycleError(
                "durable resume marker ancestry is incomplete",
                context={"run_id": identity.run_id},
            )
        parent = await _checkpoint_parent(checkpointer, current)
        parent_committed = _committed_resume_marker_for_identity(
            parent,
            identity=identity,
        )
        if parent_committed is None:
            parent_marker = _resume_marker_for_identity(
                parent,
                identity=identity,
                runtime_profile=runtime_profile,
            )
            if parent_marker != marker:
                raise TinkerFinLifecycleError(
                    "durable resume intent lost its pending-write source",
                    context={"run_id": identity.run_id},
                )
            return current, parent
        if parent_committed != marker:
            raise TinkerFinLifecycleError(
                "durable resume ancestry contains a conflicting marker",
                context={"run_id": identity.run_id},
            )
        current = parent
    raise TinkerFinLifecycleError(
        "durable resume ancestry exceeds the safe checkpoint depth",
        context={"run_id": identity.run_id},
        diagnostic_context={"maximum_depth": _MAX_RESUME_ANCESTRY_DEPTH},
    )


async def _durable_resume_progress(
    checkpointer: _CheckpointSaver,
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    resume: AgUiResumeBinding,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> _DurableResumeProgress | None:
    """Resolve a prepared or already-submitted resume from the canonical head."""

    head = await _thread_head(checkpointer, thread_id=identity.thread_id)
    if head is None:
        return None
    marker = _resume_marker_for_identity(
        head,
        identity=identity,
        runtime_profile=runtime_profile,
    )
    if marker is None:
        return None
    effective_parent = marker.parent_run_id
    if parent_run_id is not None and parent_run_id != effective_parent:
        raise TinkerFinLifecycleError(
            "durable resume marker belongs to another parentRunId",
            context={"run_id": identity.run_id},
        )
    expected = resume._marker(
        identity=identity,
        parent_run_id=effective_parent,
    )
    if marker != expected:
        raise TinkerFinLifecycleError(
            "runId already owns a different durable resume marker",
            context={"run_id": identity.run_id},
        )
    if _committed_resume_marker_for_identity(
        head, identity=identity
    ) is None and not _pending_resume_lineage_ready(
        head,
        identity=identity,
        parent_run_id=effective_parent,
        runtime_profile=runtime_profile,
    ):
        return None
    stage, source = await _resume_stage_and_source(
        checkpointer,
        head=head,
        identity=identity,
        marker=marker,
        runtime_profile=runtime_profile,
    )
    phase: Literal["prepared", "accepted"] = (
        "accepted"
        if _committed_resume_marker_for_identity(head, identity=identity) == marker
        or runtime_profile.native_resume_submitted(head)
        else "prepared"
    )
    return _DurableResumeProgress(
        head=head,
        stage=stage,
        source=source,
        marker=marker,
        phase=phase,
    )


async def _resume_source_from_head(
    checkpointer: _CheckpointSaver,
    *,
    head: CheckpointTuple,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
) -> CheckpointTuple | None:
    """Recover the interrupted source used to rebuild a retried binding.

    A prepared intent remains on the interrupted source and can be read directly. Once
    the marker is committed, its first ancestor carrying the Profile-owned pending write
    identifies the original interrupt without trusting list order.
    """

    marker = _resume_marker_for_identity(
        head,
        identity=identity,
        runtime_profile=runtime_profile,
    )
    if marker is None:
        return None
    _stage, source = await _resume_stage_and_source(
        checkpointer,
        head=head,
        identity=identity,
        marker=marker,
        runtime_profile=runtime_profile,
    )
    source_lineage = _checkpoint_lineage(source)
    if source_lineage is None:
        raise TinkerFinLifecycleError(
            "durable resume intent source has no private lineage marker"
        )
    if (
        marker.parent_run_id is not None
        and source_lineage.run_id != marker.parent_run_id
    ):
        raise TinkerFinLifecycleError(
            "durable resume intent source conflicts with parentRunId",
            context={"run_id": identity.run_id},
        )
    return source


def _require_checkpointer(astream: Callable[..., object]) -> _CheckpointSaver:
    owner = getattr(astream, "__self__", None)
    checkpointer = getattr(owner, "checkpointer", None)
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise TinkerFinLifecycleError(
            "AG-UI branching and resume require a concrete BaseCheckpointSaver"
        )
    return cast(_CheckpointSaver, checkpointer)


async def _validate_branch_source(
    astream: Callable[..., object],
    checkpoint: CheckpointTuple,
    *,
    parent_run_id: str | None,
    runtime_profile: str,
    resume_interrupt_ids: frozenset[str] | None,
) -> None:
    """Prove a checkpoint is safe for the requested branch or exact resume.

    Runtime Profile ownership is checked before state inspection or Graph continuation.
    A resume must address exactly the pending interrupt set, while an ordinary branch
    requires a completed, non-interrupted snapshot with no failed task evidence.
    """

    _require_runtime_profile(checkpoint, expected=runtime_profile)
    snapshot = await _read_snapshot(astream, checkpoint)
    task_errors = tuple(task.error for task in snapshot.tasks if task.error is not None)
    interrupts = _interrupt_ids(snapshot)
    context = {} if parent_run_id is None else {"parent_run_id": parent_run_id}
    if task_errors:
        raise TinkerFinLifecycleError(
            "a failed checkpoint cannot be used as a branch or resume source",
            context=context,
            diagnostic_context={"failed_task_count": len(task_errors)},
        )
    if resume_interrupt_ids is not None:
        if interrupts != resume_interrupt_ids:
            raise TinkerFinLifecycleError(
                "a resume requires the source checkpoint's exact interrupts",
                context=context,
            )
        return
    if interrupts:
        raise TinkerFinLifecycleError(
            "an interrupted parent requires a resume for its exact interrupts",
            context=context,
        )
    if snapshot.next:
        raise TinkerFinLifecycleError(
            "an active parent run cannot be used as a branch source",
            context=context,
        )


async def bind_agui_lineage(
    astream: Callable[..., object],
    config: RunnableConfig,
    *,
    identity: RunIdentity,
    parent_run_id: str | None,
    runtime_profile: DeepAgentsRuntimeProfile,
    resume: AgUiResumeBinding | None,
) -> AgUiLineageResolution:
    """Resolve canonical branch lineage and durable resume progress.

    A parent must identify the unique completed root checkpoint leaf for an ordinary
    branch. An interrupted parent is valid only when the request carries a resume
    binding for the exact native interrupt set. A durable marker distinguishes a
    prepared intent from a continuation whose decision was already submitted.

    Args:
        astream: Bound graph stream whose owner exposes the saver and state reader.
        config: Canonical execution-thread configuration for this invocation.
        identity: Canonical public, Graph, checkpoint, and delivery identity.
        parent_run_id: Optional branch or resume source in the same thread.
        runtime_profile: Profile that must own every selected checkpoint.
        resume: Validated native resume binding, or ``None`` for an ordinary run.

    Returns:
        The selected checkpoint, effective parent lineage, and resume phase.

    Raises:
        TinkerFinLifecycleError: The parent is missing, ambiguous, active, failed,
            interrupted without a matching resume, or cannot be inspected safely.
    """

    updated = cast(RunnableConfig, dict(config))
    configurable = dict(updated.get("configurable", {}))
    if configurable.get("checkpoint_ns") not in (None, ""):
        raise TinkerFinLifecycleError(
            "AG-UI main runs require the root checkpoint scope"
        )
    if configurable.get("checkpoint_id") is not None:
        raise TinkerFinLifecycleError(
            "AG-UI checkpoint selection is owned by parentRunId"
        )
    configurable[RUN_ID_METADATA_KEY] = identity.run_id
    profile_id = runtime_profile.profile_id
    configured_profile = configurable.get(RUNTIME_PROFILE_METADATA_KEY)
    if configured_profile not in (None, profile_id):
        raise TinkerFinLifecycleError("Graph config belongs to another Runtime Profile")
    configurable[RUNTIME_PROFILE_METADATA_KEY] = profile_id
    # RedisSaver 0.5.1 indexes only its standard configurable run_id. Keep the
    # private metadata as the cross-saver authority and verify every listed result.
    configurable[_CHECKPOINTER_RUN_ID_KEY] = identity.run_id
    configurable[CHECKPOINT_ROLE_METADATA_KEY] = NATIVE_CHECKPOINT_ROLE
    configurable["checkpoint_ns"] = ""
    if parent_run_id is None and resume is None:
        updated["configurable"] = configurable
        return AgUiLineageResolution(
            updated,
            resume_phase="none",
            parent_run_id=None,
            checkpoint_role=NATIVE_CHECKPOINT_ROLE,
        )

    checkpointer = _require_checkpointer(astream)
    if resume is not None:
        progress = await _durable_resume_progress(
            checkpointer,
            identity=identity,
            parent_run_id=parent_run_id,
            resume=resume,
            runtime_profile=runtime_profile,
        )
        if progress is not None:
            _require_runtime_profile(progress.head, expected=profile_id)
            effective_parent = progress.marker.parent_run_id
            if effective_parent is not None:
                configurable[PARENT_RUN_ID_METADATA_KEY] = effective_parent
            if progress.phase == "prepared":
                configurable["checkpoint_id"] = _checkpoint_id(progress.head)
            updated["configurable"] = configurable
            return AgUiLineageResolution(
                updated,
                resume_phase=progress.phase,
                parent_run_id=effective_parent,
                checkpoint_role=cast(
                    LineageRole,
                    _checkpoint_role(progress.head),
                ),
            )

    if parent_run_id is None:
        source = await _thread_head(
            checkpointer,
            thread_id=identity.thread_id,
        )
        if source is None:
            raise TinkerFinLifecycleError(
                "resume has no checkpoint in this canonical thread"
            )
    else:
        source = await _run_head(
            checkpointer,
            thread_id=identity.thread_id,
            run_id=parent_run_id,
        )
    await _validate_branch_source(
        astream,
        source,
        parent_run_id=parent_run_id,
        runtime_profile=profile_id,
        resume_interrupt_ids=(
            None if resume is None else frozenset(resume.native_interrupt_ids)
        ),
    )
    if resume is not None and not resume.native_interrupt_ids:
        raise TinkerFinLifecycleError(
            "resume binding contains no native interrupt identities"
        )

    if resume is not None:
        source_lineage = _checkpoint_lineage(source)
        if source_lineage is None:
            raise TinkerFinLifecycleError("resume source has no private lineage marker")
        effective_parent = source_lineage.run_id
        if parent_run_id is not None and effective_parent != parent_run_id:
            raise TinkerFinLifecycleError(
                "resume source conflicts with parentRunId",
                context={"parent_run_id": parent_run_id},
            )
        if effective_parent == identity.run_id:
            raise TinkerFinLifecycleError(
                "resume runId cannot own its interrupted source"
            )
        configurable["checkpoint_id"] = _checkpoint_id(source)
        configurable[PARENT_RUN_ID_METADATA_KEY] = effective_parent
        phase: Literal["none", "unstaged"] = "unstaged"
    else:
        effective_parent = parent_run_id
        phase = "none"
        if parent_run_id is not None:
            configurable["checkpoint_id"] = _checkpoint_id(source)
            configurable[PARENT_RUN_ID_METADATA_KEY] = parent_run_id
    updated["configurable"] = configurable
    return AgUiLineageResolution(
        updated,
        resume_phase=phase,
        parent_run_id=effective_parent,
        checkpoint_role=cast(LineageRole, _checkpoint_role(source)),
    )


async def stage_agui_resume_intent(
    astream: Callable[..., object],
    resolution: AgUiLineageResolution,
    *,
    identity: RunIdentity,
    runtime_profile: DeepAgentsRuntimeProfile,
    resume: AgUiResumeBinding,
    state_update: Mapping[str, object],
) -> AgUiLineageResolution:
    """Commit and verify a private resume intent before decision submission.

    The selected Profile writes the private channels through the borrowed saver without
    creating a Graph checkpoint or executing a node. This preserves root, Planning, and
    nested subgraph interrupt control while establishing a durable callback boundary.

    Args:
        astream: Profile-bound Graph stream exposing the interrupted saver.
        resolution: Verified unstaged lineage and exact source configuration.
        identity: Resume Run identity written into private lineage.
        runtime_profile: Profile owning saver-specific private-write semantics.
        resume: Validated decisions used to derive the private marker.
        state_update: Exact private lineage and resume-marker channel values.

    Returns:
        Resolution whose marker is proven saver-readable in the prepared phase.

    Raises:
        asyncio.CancelledError: Caller cancellation after the retained write settles.
        TinkerFinLifecycleError: The source, private writes, or durable evidence is
            missing, conflicting, or owned by another Profile.
    """

    if resolution.resume_phase != "unstaged":
        raise TinkerFinLifecycleError(
            "resume intent can be staged only from an interrupted source"
        )
    checkpointer = _require_checkpointer(astream)
    source = await checkpointer.aget_tuple(resolution.config)
    if source is None:
        raise TinkerFinLifecycleError("resume source checkpoint is unavailable")
    required_channels = (LINEAGE_STATE_KEY, RESUME_MARKER_STATE_KEY)
    if set(state_update) != set(required_channels):
        raise TinkerFinLifecycleError(
            "resume intent must contain only private lineage and marker state"
        )
    writes = tuple((channel, state_update[channel]) for channel in required_channels)

    async def stage() -> BaseException | None:
        try:
            await runtime_profile.stage_resume_intent(
                checkpointer,
                resolution.config,
                writes,
            )
        except BaseException as error:  # noqa: BLE001 - return control to owner task
            return error
        return None

    stage_task = asyncio.create_task(
        stage(),
        name="tinkerfin-resume-intent-stage",
    )
    # Saver writes must settle before caller cancellation can classify the marker;
    # abandoning an in-flight write would make host claim release unknowable.
    stage_error = await join_task(stage_task)
    if isinstance(stage_error, Exception):
        raise TinkerFinLifecycleError(
            "Runtime Profile could not durably stage the resume intent",
            cause=stage_error,
        ) from stage_error
    if stage_error is not None:
        raise stage_error
    progress = await _durable_resume_progress(
        checkpointer,
        identity=identity,
        parent_run_id=resolution.parent_run_id,
        resume=resume,
        runtime_profile=runtime_profile,
    )
    if progress is None or progress.phase != "prepared":
        raise TinkerFinLifecycleError(
            "resume intent was not durably prepared before decision submission"
        )
    if _checkpoint_id(progress.stage) != _checkpoint_id(source):
        raise TinkerFinLifecycleError(
            "Profile staged the resume intent on another checkpoint"
        )
    _require_runtime_profile(
        progress.stage,
        expected=runtime_profile.profile_id,
    )
    return AgUiLineageResolution(
        resolution.config,
        resume_phase="prepared",
        parent_run_id=progress.marker.parent_run_id,
        checkpoint_role=resolution.checkpoint_role,
    )


__all__ = [
    "CHECKPOINT_ROLE_METADATA_KEY",
    "NATIVE_CHECKPOINT_ROLE",
    "PARENT_RUN_ID_METADATA_KEY",
    "PLANNING_CHECKPOINT_ROLE",
    "RUNTIME_PROFILE_METADATA_KEY",
    "RUN_ID_METADATA_KEY",
    "AgUiLineageResolution",
    "AgUiResumeContext",
    "AgUiThreadHead",
    "bind_agui_lineage",
    "resolve_agui_native_run_head",
    "resolve_agui_resume_context",
    "resolve_agui_thread_head",
    "stage_agui_resume_intent",
]
