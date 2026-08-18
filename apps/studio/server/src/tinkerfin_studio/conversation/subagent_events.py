"""为 Studio 子 Agent 扩展补充服务端身份"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import BaseEvent, RawEvent
from pydantic import JsonValue


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} 必须是非空且无首尾空白的字符串")
    return value


def _namespace(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("子 Agent namespace 必须是非空数组")
    return tuple(_required_text(item, "namespace 项") for item in value)


@dataclass(frozen=True, slots=True)
class _SubagentRun:
    namespace: tuple[str, ...]
    run_id: str
    graph_task_id: str
    agent_name: str
    parent_tool_call_id: str
    description: str

    def public_descriptor(self, *, parent_run_id: str) -> dict[str, JsonValue]:
        return {
            "namespace": list(self.namespace),
            "graphTaskId": self.graph_task_id,
            "agentName": self.agent_name,
            "parentToolCallId": self.parent_tool_call_id,
            "description": self.description,
            "runId": self.run_id,
            "parentAgentRunId": parent_run_id,
        }


class SubagentRunEventEnricher:
    """用完整 namespace 为一次主 run 的子 Agent 生成稳定服务端身份"""

    def __init__(self, *, user_id: int, thread_id: str, main_run_id: str) -> None:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id 必须是正整数")
        self._user_id = user_id
        self._thread_id = _required_text(thread_id, "thread_id")
        self._main_run_id = _required_text(main_run_id, "main_run_id")
        self._runs: dict[tuple[str, ...], _SubagentRun] = {}

    def __call__(self, event: BaseEvent) -> BaseEvent:
        """补充 RAW task 描述、子流事件和父 task 结果的身份关联"""

        if isinstance(event, RawEvent):
            return self._enrich_task_start(event)
        raw_event = getattr(event, "raw_event", None)
        if not isinstance(raw_event, dict):
            return event
        updated = dict(raw_event)
        source = updated.get("source")
        if isinstance(source, dict) and source.get("agentType") == "subagent":
            run = self._run_from_source(source)
            updated["runId"] = run.run_id
            updated["parentAgentRunId"] = self._main_run_id
        related_namespace = updated.get("relatedNamespace")
        if isinstance(related_namespace, list):
            run = self._runs.get(_namespace(related_namespace))
            if run is not None:
                updated["relatedRunId"] = run.run_id
        if updated == raw_event:
            return event
        return event.model_copy(update={"raw_event": updated})

    def _enrich_task_start(self, event: RawEvent) -> BaseEvent:
        raw_event = event.raw_event
        if (
            event.source != "langgraph.tasks"
            or not isinstance(raw_event, dict)
            or raw_event.get("type") != "tasks"
            or raw_event.get("phase") != "start"
            or not isinstance(event.event, dict)
        ):
            return event
        provenance = event.event.get("provenance")
        if not isinstance(provenance, dict):
            return event
        values = provenance.get("subagents")
        if not isinstance(values, list) or not values:
            return event
        enriched: list[dict[str, JsonValue]] = []
        for value in values:
            if not isinstance(value, dict):
                raise TypeError("subagents 项必须是对象")
            run = self._run_from_descriptor(value)
            enriched.append(run.public_descriptor(parent_run_id=self._main_run_id))
        next_provenance = {**provenance, "subagents": enriched}
        next_event = {**event.event, "provenance": next_provenance}
        return event.model_copy(update={"event": next_event})

    def _run_from_descriptor(self, value: dict[str, object]) -> _SubagentRun:
        return self._register(
            namespace=_namespace(value.get("namespace")),
            graph_task_id=_required_text(value.get("graphTaskId"), "graphTaskId"),
            agent_name=_required_text(value.get("agentName"), "agentName"),
            parent_tool_call_id=_required_text(
                value.get("parentToolCallId"), "parentToolCallId"
            ),
            description=_required_text(value.get("description"), "description"),
        )

    def _run_from_source(self, source: dict[object, object]) -> _SubagentRun:
        return self._register(
            namespace=_namespace(source.get("namespace")),
            graph_task_id=_required_text(source.get("graphTaskId"), "graphTaskId"),
            agent_name=_required_text(source.get("agentName"), "agentName"),
            parent_tool_call_id=_required_text(
                source.get("parentToolCallId"), "parentToolCallId"
            ),
            description=_required_text(source.get("subagentInput"), "subagentInput"),
        )

    def _register(
        self,
        *,
        namespace: tuple[str, ...],
        graph_task_id: str,
        agent_name: str,
        parent_tool_call_id: str,
        description: str,
    ) -> _SubagentRun:
        namespace_json = json.dumps(
            namespace,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        run_id = f"subrun-{uuid5(NAMESPACE_URL, f'tinkerfin-studio:{self._user_id}:{self._thread_id}:{namespace_json}')}"
        candidate = _SubagentRun(
            namespace=namespace,
            run_id=run_id,
            graph_task_id=graph_task_id,
            agent_name=agent_name,
            parent_tool_call_id=parent_tool_call_id,
            description=description,
        )
        existing = self._runs.get(namespace)
        if existing is not None and existing != candidate:
            raise ValueError(f"子 Agent namespace 身份冲突: {namespace!r}")
        self._runs[namespace] = candidate
        return cast(_SubagentRun, existing or candidate)
