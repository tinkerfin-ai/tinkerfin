"""Checkpoint lineage owned by the public AG-UI run boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.types import StateSnapshot
from pydantic import ValidationError

from tinkerfin_agui_adapter import Identity

from ._agui_lineage_state import (
    LINEAGE_STATE_KEY,
    PLANNING_CHECKPOINT_RUN_ID,
    LineageMarker,
    LineageRole,
    parse_lineage_marker,
)
from .agui_resume import (
    RESUME_MARKER_STATE_KEY,
    AgUiResumeBinding,
    _ResumeMarker,
    parse_resume_marker,
)
from .errors import TinkerFinLifecycleError

RUN_ID_METADATA_KEY = "_tinkerfin_run_id"
CHECKPOINT_ROLE_METADATA_KEY = "_tinkerfin_checkpoint_role"
PARENT_RUN_ID_METADATA_KEY = "_tinkerfin_parent_run_id"
NATIVE_CHECKPOINT_ROLE = "native"
PLANNING_CHECKPOINT_ROLE = "planning"
_CHECKPOINTER_RUN_ID_KEY = "run_id"

_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
_SnapshotReader: TypeAlias = Callable[[RunnableConfig], Awaitable[StateSnapshot]]
_RoleSnapshotReader: TypeAlias = Callable[
    [RunnableConfig, str], Awaitable[StateSnapshot]
]


@dataclass(frozen=True, slots=True)
class AgUiLineageResolution:
    """Resolved invocation checkpoint and durable-resume retry state."""

    config: RunnableConfig
    resume_checkpointed: bool


@dataclass(frozen=True, slots=True)
class AgUiThreadHead:
    """Saver-neutral canonical thread head and its owning Graph role."""

    config: RunnableConfig
    role: LineageRole


def _checkpoint_id(checkpoint: CheckpointTuple) -> str:
    value = checkpoint.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(value, str) or not value:
        raise TinkerFinLifecycleError("AG-UI lineage checkpoint has no stable ID")
    return value


def _checkpoint_role(checkpoint: CheckpointTuple) -> str:
    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError(
            "AG-UI lineage checkpoint has no private lineage marker"
        )
    return marker.role


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
    return _unique_leaf(
        native,
        missing_message="native run has no checkpoint in this canonical thread",
        ambiguous_message="native run resolves to ambiguous checkpoint branches",
        run_id=run_id,
    )


async def _read_snapshot(
    astream: Callable[..., object],
    checkpoint: CheckpointTuple,
) -> StateSnapshot:
    owner = getattr(astream, "__self__", None)
    role = _checkpoint_role(checkpoint)
    role_reader = getattr(owner, "_tinkerfin_lineage_state", None)
    if callable(role_reader):
        return await cast(_RoleSnapshotReader, role_reader)(checkpoint.config, role)
    if role != NATIVE_CHECKPOINT_ROLE:
        raise TinkerFinLifecycleError(
            "planning checkpoint requires a Plan-capable lineage reader"
        )
    reader = getattr(owner, "aget_state", None)
    if not callable(reader):
        raise TinkerFinLifecycleError(
            "parentRunId requires a graph with asynchronous state inspection"
        )
    return await cast(_SnapshotReader, reader)(checkpoint.config)


def _interrupt_ids(snapshot: StateSnapshot) -> frozenset[str]:
    ids = frozenset(interrupt.id for interrupt in snapshot.interrupts)
    if len(ids) != len(snapshot.interrupts):
        raise TinkerFinLifecycleError(
            "parent checkpoint contains duplicate interrupt identities"
        )
    return ids


def _checkpoint_markers(checkpoint: CheckpointTuple) -> tuple[_ResumeMarker, ...]:
    raw_values: list[object] = []
    channel_values = checkpoint.checkpoint.get("channel_values")
    if (
        isinstance(channel_values, Mapping)
        and RESUME_MARKER_STATE_KEY in channel_values
    ):
        raw_values.append(
            cast(Mapping[object, object], channel_values)[RESUME_MARKER_STATE_KEY]
        )
    raw_values.extend(
        value
        for _task_id, channel, value in checkpoint.pending_writes or ()
        if channel == RESUME_MARKER_STATE_KEY
    )
    markers: list[_ResumeMarker] = []
    for value in raw_values:
        marker = parse_resume_marker(value)
        if marker is None:
            raise TinkerFinLifecycleError(
                "checkpoint contains an invalid private resume marker"
            )
        if marker not in markers:
            markers.append(marker)
    return tuple(markers)


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
    marker = _checkpoint_lineage(checkpoint)
    if marker is None:
        raise TinkerFinLifecycleError("AG-UI thread head has no private lineage marker")
    if marker.thread_id != thread_id:
        raise TinkerFinLifecycleError(
            "AG-UI thread head lineage belongs to a different thread"
        )
    return AgUiThreadHead(config=checkpoint.config, role=marker.role)


async def _resume_marker_head(
    checkpointer: _CheckpointSaver,
    *,
    identity: Identity,
    parent_run_id: str | None,
    resume: AgUiResumeBinding,
) -> CheckpointTuple | None:
    """Find the unique checkpoint proving this exact resume request."""

    expected = resume._marker(identity=identity, parent_run_id=parent_run_id)
    candidates_by_id: dict[str, CheckpointTuple] = {}
    thread_id = identity.thread_id
    current_head = await _thread_head(checkpointer, thread_id=thread_id)
    if current_head is not None:
        candidates_by_id[_checkpoint_id(current_head)] = current_head
    for run_id in dict.fromkeys((identity.run_id, parent_run_id)):
        if run_id is None:
            continue
        for checkpoint in await _run_checkpoints(
            checkpointer,
            thread_id=thread_id,
            run_id=run_id,
        ):
            candidates_by_id[_checkpoint_id(checkpoint)] = checkpoint

    exact: list[CheckpointTuple] = []
    for checkpoint in candidates_by_id.values():
        for marker in _checkpoint_markers(checkpoint):
            same_identity = (
                marker.thread_id == expected.thread_id
                and marker.run_id == expected.run_id
            )
            if marker == expected:
                exact.append(checkpoint)
            elif same_identity:
                raise TinkerFinLifecycleError(
                    "runId already owns a different durable resume marker",
                    context={"run_id": expected.run_id},
                )
    if not exact:
        return None
    exact_ids = {_checkpoint_id(checkpoint) for checkpoint in exact}
    if current_head is None or _checkpoint_id(current_head) not in exact_ids:
        raise TinkerFinLifecycleError(
            "durable resume marker is no longer the canonical thread head",
            context={"run_id": expected.run_id},
        )
    return current_head


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
    resume_interrupt_ids: frozenset[str] | None,
) -> None:
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
    identity: Identity,
    parent_run_id: str | None,
    resume: AgUiResumeBinding | None,
) -> AgUiLineageResolution:
    """Resolve canonical branch lineage and durable resume continuation.

    A parent must identify the unique completed root checkpoint leaf for an ordinary
    branch. An interrupted parent is valid only when the request carries a resume
    binding for the exact native interrupt set. A previously checkpointed private
    marker changes a retry into ``astream(None)`` continuation at that exact leaf.

    Args:
        astream: Bound graph stream whose owner exposes the saver and state reader.
        config: Canonical execution-thread configuration for this invocation.
        identity: Canonical public, Graph, checkpoint, and delivery identity.
        parent_run_id: Optional branch or resume source in the same thread.
        resume: Validated native resume binding, or ``None`` for an ordinary run.

    Returns:
        The copied checkpoint configuration and whether the resume command was already
        checkpointed by a prior attempt.

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
    # RedisSaver 0.5.1 indexes only its standard configurable run_id. Keep the
    # private metadata as the cross-saver authority and verify every listed result.
    configurable[_CHECKPOINTER_RUN_ID_KEY] = identity.run_id
    configurable[CHECKPOINT_ROLE_METADATA_KEY] = NATIVE_CHECKPOINT_ROLE
    configurable["checkpoint_ns"] = ""
    if parent_run_id is None and resume is None:
        updated["configurable"] = configurable
        return AgUiLineageResolution(updated, resume_checkpointed=False)

    checkpointer = _require_checkpointer(astream)
    if resume is not None:
        checkpointed = await _resume_marker_head(
            checkpointer,
            identity=identity,
            parent_run_id=parent_run_id,
            resume=resume,
        )
        if checkpointed is not None:
            if parent_run_id is not None:
                configurable[PARENT_RUN_ID_METADATA_KEY] = parent_run_id
            updated["configurable"] = configurable
            return AgUiLineageResolution(updated, resume_checkpointed=True)

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
        resume_interrupt_ids=(
            None if resume is None else frozenset(resume.native_interrupt_ids)
        ),
    )
    if resume is not None and not resume.native_interrupt_ids:
        raise TinkerFinLifecycleError(
            "resume binding contains no native interrupt identities"
        )

    if parent_run_id is not None:
        configurable["checkpoint_id"] = _checkpoint_id(source)
        configurable[PARENT_RUN_ID_METADATA_KEY] = parent_run_id
    updated["configurable"] = configurable
    return AgUiLineageResolution(updated, resume_checkpointed=False)


async def verify_agui_resume_marker(
    astream: Callable[..., object],
    config: RunnableConfig,
    *,
    identity: Identity,
    parent_run_id: str | None,
    resume: AgUiResumeBinding,
) -> None:
    """Require atomic marker persistence before settlement or native delivery."""

    checkpointer = _require_checkpointer(astream)
    checkpoint = await checkpointer.aget_tuple(config)
    if checkpoint is None or not any(
        resume._matches_marker(
            marker,
            identity=identity,
            parent_run_id=parent_run_id,
        )
        for marker in _checkpoint_markers(checkpoint)
    ):
        raise TinkerFinLifecycleError(
            "resume command was not durably checkpointed before native delivery"
        )


__all__ = [
    "CHECKPOINT_ROLE_METADATA_KEY",
    "NATIVE_CHECKPOINT_ROLE",
    "PARENT_RUN_ID_METADATA_KEY",
    "PLANNING_CHECKPOINT_ROLE",
    "RUN_ID_METADATA_KEY",
    "AgUiLineageResolution",
    "AgUiThreadHead",
    "bind_agui_lineage",
    "resolve_agui_native_run_head",
    "resolve_agui_thread_head",
    "verify_agui_resume_marker",
]
