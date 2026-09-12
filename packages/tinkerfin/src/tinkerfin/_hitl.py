"""Version-bound Deep Agents HITL cancellation integration."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, uuid5

from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents.middleware import (
    AgentState,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from langchain.agents.middleware.human_in_the_loop import Decision, HITLRequest
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import Interrupt, interrupt
from wcmatch import glob as wcglob

from ._agui_lineage_state import RUN_ID_METADATA_KEY
from ._hitl_state import (
    TOOL_REVIEW_CHANNEL,
    PendingToolReview,
    pending_tool_review,
    tool_review_digest,
    tool_review_owner,
)
from ._tasks import run_async_owned
from .errors import TinkerFinLifecycleError

CANCEL_DECISION_TYPE = "tinkerfin_cancel"
HITL_CONTRACT_ID = "tinkerfin.deepagents.hitl-cancel"

_SUPPORTED_DEEPAGENTS_VERSION = "0.7.5"
_SUPPORTED_LANGCHAIN_VERSION = "1.3.14"
_GLOB_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR
_GLOB_WILDCARD_CHARS = frozenset("*?[") | frozenset("{")
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)

_FilesystemOperation = Literal["read", "write"]
_ToolScope = Literal["exact", "bulk"]
_FS_TOOL_PATH_ARGS: dict[
    str,
    tuple[_FilesystemOperation, str, _ToolScope, str | None],
] = {
    "ls": ("read", "path", "bulk", None),
    "read_file": ("read", "file_path", "exact", None),
    "write_file": ("write", "file_path", "exact", None),
    "edit_file": ("write", "file_path", "exact", None),
    "delete": ("write", "file_path", "bulk", None),
    "glob": ("read", "path", "bulk", "pattern"),
    "grep": ("read", "path", "bulk", None),
}


def _validate_locked_hitl_versions() -> None:
    """Fail before graph construction when the reviewed parent contract changed."""

    installed = {
        "deepagents": version("deepagents"),
        "langchain": version("langchain"),
    }
    expected = {
        "deepagents": _SUPPORTED_DEEPAGENTS_VERSION,
        "langchain": _SUPPORTED_LANGCHAIN_VERSION,
    }
    if installed != expected:
        raise TinkerFinLifecycleError(
            "TinkerFin HITL cancellation requires its locked dependency contract",
            context=installed,
            diagnostic_context=expected,
        )


def _normalize_path(path: str) -> str:
    """Normalize the virtual path shape used by Deep Agents 0.7.5 permissions."""

    posix = path.replace("\\", "/")
    parts = PurePosixPath(posix).parts
    if ".." in parts or path.startswith("~") or re.match(r"^[a-zA-Z]:", path):
        raise ValueError("path is outside the Deep Agents virtual path contract")
    normalized = os.path.normpath(path).replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if ".." in normalized.split("/"):
        raise ValueError("normalized path contains traversal")
    return normalized


def _glob_anchor(pattern: str) -> str:
    parts = PurePosixPath(pattern.replace("\\", "/")).parts
    safe: list[str] = []
    for part in parts:
        if any(character in _GLOB_WILDCARD_CHARS for character in part):
            break
        safe.append(part)
    return "/" if not safe else str(PurePosixPath(*safe))


def _paths_overlap(first: str, second: str) -> bool:
    left = PurePosixPath(first)
    right = PurePosixPath(second)
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _permission_mode(
    rules: Sequence[FilesystemPermission],
    operation: _FilesystemOperation,
    path: str,
) -> Literal["allow", "deny", "interrupt"]:
    for rule in rules:
        if operation not in rule.operations:
            continue
        if any(
            wcglob.globmatch(path, pattern, flags=_GLOB_FLAGS) for pattern in rule.paths
        ):
            return rule.mode
    return "allow"


def _exact_when(
    rules: tuple[FilesystemPermission, ...],
    operation: _FilesystemOperation,
    path_argument: str,
) -> Callable[[ToolCallRequest], bool]:
    def when(request: ToolCallRequest) -> bool:
        raw_path = request.tool_call.get("args", {}).get(path_argument)
        if not isinstance(raw_path, str):
            return False
        try:
            normalized = _normalize_path(raw_path)
        except ValueError:
            return False
        return _permission_mode(rules, operation, normalized) == "interrupt"

    return when


def _bulk_pattern_fires(raw_pattern: str, anchors: tuple[str, ...]) -> bool:
    normalized = raw_pattern.replace("\\", "/")
    if normalized.startswith("/"):
        return any(_paths_overlap(_glob_anchor(raw_pattern), item) for item in anchors)
    return ".." in PurePosixPath(normalized).parts


def _bulk_when(
    rules: tuple[FilesystemPermission, ...],
    operation: _FilesystemOperation,
    path_argument: str,
    pattern_argument: str | None,
) -> Callable[[ToolCallRequest], bool]:
    anchors = tuple(
        _glob_anchor(pattern)
        for rule in rules
        if rule.mode == "interrupt" and operation in rule.operations
        for pattern in rule.paths
    )

    def when(request: ToolCallRequest) -> bool:
        if not anchors:
            return False
        arguments = request.tool_call.get("args", {})
        raw_path = arguments.get(path_argument)
        if not isinstance(raw_path, str):
            return raw_path is None
        try:
            normalized = _normalize_path(raw_path)
        except ValueError:
            return False
        if normalized == "/.":
            normalized = "/"
        if any(_paths_overlap(normalized, anchor) for anchor in anchors):
            return True
        if pattern_argument is None:
            return False
        raw_pattern = arguments.get(pattern_argument)
        return isinstance(raw_pattern, str) and _bulk_pattern_fires(
            raw_pattern,
            anchors,
        )

    return when


def _permission_interrupts(
    rules: tuple[FilesystemPermission, ...],
) -> dict[str, InterruptOnConfig]:
    """Reproduce the reviewed Deep Agents 0.7.5 public permission behavior."""

    allowed: list[Literal["approve", "edit", "reject", "respond"]] = [
        "approve",
        "edit",
        "reject",
        "respond",
    ]
    result: dict[str, InterruptOnConfig] = {}
    for tool_name, (
        operation,
        path_argument,
        scope,
        pattern_argument,
    ) in _FS_TOOL_PATH_ARGS.items():
        if not any(
            rule.mode == "interrupt" and operation in rule.operations for rule in rules
        ):
            continue
        when = (
            _exact_when(rules, operation, path_argument)
            if scope == "exact"
            else _bulk_when(
                rules,
                operation,
                path_argument,
                pattern_argument,
            )
        )
        result[tool_name] = InterruptOnConfig(
            allowed_decisions=allowed,
            when=when,
        )
    return result


def _settled_permissions(
    rules: tuple[FilesystemPermission, ...],
) -> list[FilesystemPermission]:
    """Keep deny/allow enforcement while moving interrupt gating to HITL."""

    return [
        FilesystemPermission(
            operations=list(rule.operations),
            paths=list(rule.paths),
            mode="allow" if rule.mode == "interrupt" else rule.mode,
        )
        for rule in rules
    ]


async def _save_tool_review(
    saver: _CheckpointSaver, config: RunnableConfig, review: PendingToolReview
) -> None:
    """Finish the evidence write before publishing an interrupt or releasing resources.

    LangGraph 1.2.10 reapplies writes only for real task IDs. A distinct owner keeps
    this record out of both completed-task detection and null-task resume slots.
    The record is saver evidence; it does not add a Graph state channel.
    """

    try:
        await run_async_owned(
            lambda: saver.aput_writes(
                config,
                ((TOOL_REVIEW_CHANNEL, review.model_dump(mode="json")),),
                tool_review_owner(review.task_id),
            ),
            task_name="tinkerfin-tool-review-save",
        )
    except Exception as error:
        raise TinkerFinLifecycleError(
            "could not save pending tool review", cause=error
        ) from error


class _TinkerFinHitlPatchMiddleware(
    PatchToolCallsMiddleware,
    HumanInTheLoopMiddleware,
):
    """Preserve patching and bind asynchronous approvals to their exact tool batch."""

    @property
    def name(self) -> str:
        """Replace the Deep Agents patch slot in main and inherited subagents."""

        return PatchToolCallsMiddleware.__name__

    def __init__(
        self,
        interrupt_on: dict[str, bool | InterruptOnConfig],
    ) -> None:
        HumanInTheLoopMiddleware.__init__(self, interrupt_on=interrupt_on)

    def after_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object] | None:
        """Require async execution so pending approvals can be saved safely."""

        del state, runtime
        raise NotImplementedError("TinkerFin tool review requires async execution")

    async def aafter_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, object] | None:
        """Save each review and reject changed tool selection before consuming decisions.

        Request construction and decision processing follow LangChain 1.3.14's
        HumanInTheLoopMiddleware.after_model. The additional saver record preserves
        exact tool IDs across Runtime rebuilds; tests cover policy changes, nested
        interrupts, retries, and all native decision types.
        """

        configurable = get_config().get("configurable", {})
        message = next(
            (
                item
                for item in reversed(state["messages"])
                if isinstance(item, AIMessage)
            ),
            None,
        )
        tool_calls = [] if message is None else message.tool_calls
        request = HITLRequest(action_requests=[], review_configs=[])
        selected: dict[int, InterruptOnConfig] = {}
        for index, tool_call in enumerate(tool_calls):
            policy = self.interrupt_on.get(tool_call["name"])
            if policy is None or not self._should_interrupt(
                tool_call, policy, state, runtime
            ):
                continue
            action, review = self._create_action_and_config(
                tool_call, policy, state, runtime
            )
            request["action_requests"].append(action)
            request["review_configs"].append(review)
            selected[index] = policy
        reviewed_ids: list[str] = []
        for index in selected:
            tool_call_id = tool_calls[index].get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("tool review requires stable tool call IDs")
            reviewed_ids.append(tool_call_id)
        digest = tool_review_digest(
            request,
            tool_calls=tool_calls,
            reviewed_ids=reviewed_ids,
        )
        execution = runtime.execution_info
        saver_value = configurable.get("__pregel_checkpointer")
        saver: _CheckpointSaver | None = None
        source_config: RunnableConfig = {}
        graph_namespace = ""
        if isinstance(saver_value, BaseCheckpointSaver) and execution is not None:
            if configurable.get("__pregel_durability") != "sync":
                raise ValueError("TinkerFin tool review requires durability='sync'")
            saver = cast(_CheckpointSaver, saver_value)
            # prepare_single_task adds a final node/task component. Retain every
            # parent Graph component and invocation slot when locating its saver.
            graph_namespace = execution.checkpoint_ns.rpartition("|")[0]
            source_config = {
                "configurable": {
                    "thread_id": configurable["thread_id"],
                    "checkpoint_ns": graph_namespace,
                    "checkpoint_id": execution.checkpoint_id,
                }
            }
            checkpoint = await saver.aget_tuple(source_config)
            if checkpoint is None:
                raise TinkerFinLifecycleError(
                    "tool review source checkpoint is unavailable"
                )
            saved = pending_tool_review(checkpoint, task_id=execution.task_id)
            # A changed policy may now select no tools. Check before the empty
            # request return, otherwise previously cancelled actions could execute.
            if saved is not None and (
                saved.task_id != execution.task_id
                or saved.graph_namespace != graph_namespace
                or saved.batch_digest != digest
            ):
                raise TinkerFinLifecycleError(
                    "tool review changed while awaiting a decision"
                )
        if not selected:
            return None
        try:
            response: object = interrupt(request)
        except GraphInterrupt as paused:
            if saver is None or execution is None:
                raise TinkerFinLifecycleError(
                    "tool review requires an asynchronous checkpointer"
                ) from paused
            pending = cast(Sequence[Interrupt], paused.args[0])
            if len(pending) != 1:
                raise TinkerFinLifecycleError(
                    "tool review requires one native interrupt group"
                ) from paused
            run_id = configurable.get(RUN_ID_METADATA_KEY)
            record = PendingToolReview(
                graph_namespace=graph_namespace,
                run_id=run_id if isinstance(run_id, str) else None,
                task_id=execution.task_id,
                interrupt_id=pending[0].id,
                batch_digest=digest,
            )
            await _save_tool_review(saver, source_config, record)
            raise
        if not isinstance(response, Mapping):
            raise TypeError("tool review decisions must be a mapping")
        decisions_value = cast(Mapping[object, object], response).get("decisions")
        if not isinstance(decisions_value, list):
            raise TypeError("tool review decisions must be a list")
        decisions = cast(list[Decision], decisions_value)
        if len(decisions) != len(selected):
            raise ValueError(
                "tool review decisions must cover the complete action batch"
            )
        revised: list[ToolCall] = []
        tool_messages: list[ToolMessage] = []
        decision_index = 0
        for index, tool_call in enumerate(tool_calls):
            policy = selected.get(index)
            if policy is None:
                revised.append(tool_call)
                continue
            updated, result = self._process_decision(
                decisions[decision_index], tool_call, policy
            )
            decision_index += 1
            if updated is not None:
                revised.append(updated)
            if result is not None:
                tool_messages.append(result)
        assert message is not None
        message.tool_calls = revised
        return {"messages": [message, *tool_messages]}

    @staticmethod
    def _process_decision(
        decision: Decision,
        tool_call: ToolCall,
        config: InterruptOnConfig,
    ) -> tuple[ToolCall | None, ToolMessage | None]:
        raw_decision = cast(Mapping[str, object], decision)
        if raw_decision.get("type") != CANCEL_DECISION_TYPE:
            return HumanInTheLoopMiddleware._process_decision(
                decision,
                tool_call,
                config,
            )
        tool_call_id = tool_call.get("id")
        tool_name = tool_call.get("name")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("cancelled Tool call requires a stable ID")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("cancelled Tool call requires a stable name")
        message = ToolMessage(
            content=f"Tool call `{tool_name}` was cancelled before execution.",
            name=tool_name,
            tool_call_id=tool_call_id,
            id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"{HITL_CONTRACT_ID}:{tool_name}:{tool_call_id}",
                )
            ),
            status="error",
            additional_kwargs={
                "tinkerfin": {
                    "schema": HITL_CONTRACT_ID,
                    "outcome": "cancelled",
                    "executed": False,
                }
            },
        )
        return None, message


def _as_permissions(value: object) -> tuple[FilesystemPermission, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("permissions must be a sequence or None")
    rules = tuple(cast(Sequence[object], value))
    if any(not isinstance(rule, FilesystemPermission) for rule in rules):
        raise TypeError("permissions must contain FilesystemPermission values")
    return cast(tuple[FilesystemPermission, ...], rules)


def _as_interrupt_on(value: object) -> dict[str, bool | InterruptOnConfig]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("interrupt_on must be a mapping or None")
    return dict(cast(Mapping[str, bool | InterruptOnConfig], value))


def _adapter_for(
    permissions: tuple[FilesystemPermission, ...],
    interrupt_on: dict[str, bool | InterruptOnConfig],
) -> _TinkerFinHitlPatchMiddleware | None:
    merged: dict[str, bool | InterruptOnConfig] = {
        **_permission_interrupts(permissions),
        **interrupt_on,
    }
    if not merged:
        return None
    adapter = _TinkerFinHitlPatchMiddleware(merged)
    return adapter if adapter.interrupt_on else None


def _inject_adapter(
    middleware: object,
    adapter: _TinkerFinHitlPatchMiddleware | None,
) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
    if middleware is None:
        current: tuple[AgentMiddleware[Any, Any, Any], ...] = ()
    elif isinstance(middleware, Sequence) and not isinstance(middleware, (str, bytes)):
        current = tuple(cast(Sequence[AgentMiddleware[Any, Any, Any]], middleware))
    else:
        raise TypeError("middleware must be a sequence")
    if adapter is None:
        return current
    if any(item.name == PatchToolCallsMiddleware.__name__ for item in current):
        raise TinkerFinLifecycleError(
            "TinkerFin HITL cancellation owns the PatchToolCallsMiddleware slot"
        )
    if any(isinstance(item, HumanInTheLoopMiddleware) for item in current):
        raise TinkerFinLifecycleError(
            "configure Tool review through interrupt_on, not custom HITL middleware"
        )
    return (*current, adapter)


def _prepare_declarative_subagents(
    value: object,
    *,
    inherited_permissions: tuple[FilesystemPermission, ...],
    inherited_interrupt_on: dict[str, bool | InterruptOnConfig],
) -> tuple[object, bool]:
    if value is None:
        return None, False
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("subagents must be a sequence or None")
    prepared: list[object] = []
    has_adapter = False
    for raw_spec in cast(Sequence[object], value):
        if not isinstance(raw_spec, Mapping):
            raise TypeError("subagents must contain mapping specifications")
        spec = dict(cast(Mapping[str, object], raw_spec))
        if "runnable" in spec or "graph_id" in spec:
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("external subagent requires a stable name")
            if "tinkerfin_hitl_contract" in spec:
                raise ValueError(
                    "tool cancellation requires an installed TinkerFin tool review "
                    "implementation; a subagent declaration cannot grant support"
                )
            prepared.append(spec)
            continue
        permissions = (
            _as_permissions(spec.get("permissions"))
            if "permissions" in spec
            else inherited_permissions
        )
        interrupt_on = (
            _as_interrupt_on(spec.get("interrupt_on"))
            if "interrupt_on" in spec
            else inherited_interrupt_on
        )
        adapter = _adapter_for(permissions, interrupt_on)
        has_adapter = has_adapter or adapter is not None
        spec["permissions"] = _settled_permissions(permissions)
        spec["interrupt_on"] = {}
        spec["middleware"] = list(_inject_adapter(spec.get("middleware", ()), adapter))
        prepared.append(spec)
    return prepared, has_adapter


@dataclass(frozen=True, slots=True)
class HitlFactoryOverrides:
    """Factory arguments with one cancellation-aware HITL owner per graph."""

    interrupt_on: None
    middleware: tuple[AgentMiddleware[Any, Any, Any], ...]
    permissions: list[FilesystemPermission]
    subagents: object


def prepare_hitl_factory_overrides(
    arguments: Mapping[str, object],
) -> HitlFactoryOverrides:
    """Prepare main, general-purpose, and declarative subagent HITL stacks.

    The permission predicates mirror the locked Deep Agents 0.7.5
    ``_build_interrupt_on_from_permissions`` contract. Contract tests compare exact,
    bulk, glob-pattern, precedence, and pathless behavior before dependency upgrades.

    Args:
        arguments: Build arguments already bound to the Profile factory signature.

    Returns:
        Immutable effective tool review and permission inputs.

    Raises:
        TypeError: A permission, middleware, or subagent value has the wrong shape.
        ValueError: Permission and interrupt policies conflict or are incomplete.
    """

    permissions = _as_permissions(arguments.get("permissions"))
    interrupt_on = _as_interrupt_on(arguments.get("interrupt_on"))
    adapter = _adapter_for(permissions, interrupt_on)
    prepared_subagents, has_subagent_adapter = _prepare_declarative_subagents(
        arguments.get("subagents"),
        inherited_permissions=permissions,
        inherited_interrupt_on=interrupt_on,
    )
    if adapter is not None or has_subagent_adapter:
        _validate_locked_hitl_versions()
    return HitlFactoryOverrides(
        interrupt_on=None,
        middleware=_inject_adapter(arguments.get("middleware", ()), adapter),
        permissions=_settled_permissions(permissions),
        subagents=prepared_subagents,
    )


__all__ = [
    "CANCEL_DECISION_TYPE",
    "HITL_CONTRACT_ID",
    "HitlFactoryOverrides",
    "prepare_hitl_factory_overrides",
]
