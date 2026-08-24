"""Request-local routing between standalone Planning and native Deep Agent graphs."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Protocol, TypeAlias, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from ._content import PlanContentBinding
from ._state import (
    PLAN_CHECKPOINT_RUN_ID,
    PLAN_STATE_KEY,
    read_plan_state,
)
from ._workflow import PlanningWorkflowGraph
from .errors import PlanModeConfigurationError
from .models import MarkdownPlanContent, PlanContentModel, PlanState, PlanStatus

_APPROVED_PLAN_MARKER = "<tinkerfin-approved-plan"
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)


class _GraphRuntime(Protocol):
    checkpointer: object

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...


class _SignatureCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


_COMPILED_ASTREAM = cast(
    _SignatureCallable,
    CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
)


def _resume_data(value: object) -> Mapping[object, object] | None:
    if not isinstance(value, Command):
        return None
    command = cast(Command[object], value)
    raw_resume = cast(object, command.resume)
    if not isinstance(raw_resume, Mapping):
        return None
    return cast(Mapping[object, object], raw_resume)


def _contains_decisions(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    decisions = cast(object, mapping.get("decisions"))
    return isinstance(decisions, Sequence) and not isinstance(decisions, (str, bytes))


def _is_tool_resume(value: object) -> bool:
    resume = _resume_data(value)
    if resume is None:
        return False
    if _contains_decisions(resume):
        return True
    return bool(resume) and all(_contains_decisions(item) for item in resume.values())


def _is_plan_resume(value: object) -> bool:
    return _resume_data(value) is not None and not _is_tool_resume(value)


def _config(bound: inspect.BoundArguments) -> RunnableConfig:
    value = bound.arguments.get("config")
    if value is None:
        return RunnableConfig()
    if not isinstance(value, Mapping):
        raise TypeError("config must be a mapping or None")
    return cast(RunnableConfig, dict(cast(Mapping[str, object], value)))


async def _checkpoint_state(
    planning: PlanningWorkflowGraph[Any],
    config: RunnableConfig,
) -> dict[str, object]:
    return await _plan_checkpoint_channels(planning.checkpointer, config)


async def _plan_checkpoint_channels(
    checkpointer: object,
    config: RunnableConfig,
) -> dict[str, object]:
    if not isinstance(checkpointer, BaseCheckpointSaver):
        return {}
    saver = cast(_CheckpointSaver, checkpointer)
    async for checkpoint in saver.alist(
        config,
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
        if handoff is None or not handoff.dispatched:
            raise RuntimeError("approved Plan has not committed its native dispatch")
        return plan
    return plan if plan.status is PlanStatus.CANCELLED else None


def _handoff_text(
    plan: PlanState[PlanContentModel],
    content_binding: PlanContentBinding,
) -> str:
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
        f'schema="{confirmed.content_schema.id}" '
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
        "_planning_factory",
        "_prefer_plan",
        "_signature",
    )

    def __init__(
        self,
        *,
        content: PlanContentBinding,
        native: _GraphRuntime,
        planning_factory: Callable[[], PlanningWorkflowGraph[Any]],
        prefer_plan: bool,
    ) -> None:
        self._content = content
        self._native = native
        self._planning_factory = planning_factory
        self._prefer_plan = prefer_plan
        self._signature = inspect.signature(native.astream)

    @property
    def checkpointer(self) -> object:
        """Expose the native graph checkpointer without taking ownership."""

        return self._native.checkpointer

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
        if _is_tool_resume(graph_input) or (
            not self._prefer_plan and not _is_plan_resume(graph_input)
        ):
            overlay = await _native_plan_overlay(
                self._native,
                _config(bound),
                self._content,
            )
            async for part in self._native.astream(*bound.args, **bound.kwargs):
                yield part if overlay is None else _overlay_plan(part, overlay)
            return

        planning = self._planning_factory()
        checkpoint_state = await _checkpoint_state(planning, _config(bound))
        checkpoint_plan = (
            read_plan_state(checkpoint_state, self._content)
            if PLAN_STATE_KEY in checkpoint_state
            else None
        )

        final_state: Mapping[str, object] = checkpoint_state
        final_plan = checkpoint_plan
        if (
            not isinstance(graph_input, Command)
            or checkpoint_plan is None
            or checkpoint_plan.status not in {PlanStatus.APPROVED, PlanStatus.CANCELLED}
        ):
            interrupted = False
            async for part in planning.astream(*bound.args, **bound.kwargs):
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
        if final_plan.handoff.dispatched:
            yield {
                "type": "values",
                "ns": (),
                "data": {
                    PLAN_STATE_KEY: final_plan.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=False,
                    )
                },
                "interrupts": (),
            }
            return
        message = _handoff_message(final_state, final_plan, self._content)
        if message is None:
            raise RuntimeError("undispatched Plan already appears in native messages")
        final_plan = await planning.mark_handoff_dispatched(
            _config(bound),
            final_plan,
        )

        native_bound = self._signature.bind(*bound.args, **bound.kwargs)
        native_bound.arguments["input"] = {"messages": [message]}
        durability = native_bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise PlanModeConfigurationError("Plan handoff requires durability='sync'")
        native_bound.arguments["durability"] = "sync"
        async for part in self._native.astream(
            *native_bound.args,
            **native_bound.kwargs,
        ):
            yield _overlay_plan(part, final_plan)


setattr(
    PlanCapableGraphRuntime.astream,
    "__signature__",
    inspect.signature(_COMPILED_ASTREAM),
)


__all__ = ["PlanCapableGraphRuntime"]
