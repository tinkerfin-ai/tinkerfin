"""AG-UI 事件到前端 v2 快照的纯数据归约"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import cast

import jsonpatch
from ag_ui.core import BaseEvent


def empty_snapshot() -> dict[str, object]:
    """创建一个可直接返回前端的空 v2 快照"""

    return {
        "snapshotSeq": 0,
        "snapshotVersion": 2,
        "messages": [],
        "todos": [],
        "approval": None,
        "runStatus": "idle",
        "activeRunId": None,
        "serverState": {},
        "runs": {},
        "activities": [],
        "interrupts": [],
    }


def _messages(snapshot: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], snapshot["messages"])


def _find_message(
    snapshot: dict[str, object], message_id: str
) -> dict[str, object] | None:
    return next(
        (message for message in _messages(snapshot) if message.get("id") == message_id),
        None,
    )


def _find_subagent(
    snapshot: dict[str, object], run_id: str
) -> dict[str, object] | None:
    for message in _messages(snapshot):
        meta = message.get("meta")
        if (
            message.get("role") == "subagent"
            and isinstance(meta, dict)
            and meta.get("subRunId") == run_id
        ):
            return message
    return None


def _ensure_subagent(
    snapshot: dict[str, object],
    *,
    run_id: str,
    parent_run_id: str,
    agent_name: str,
    graph_task_id: str,
    parent_tool_call_id: str,
    description: str,
    now: str,
) -> dict[str, object]:
    existing = _find_subagent(snapshot, run_id)
    if existing is not None:
        return existing
    message: dict[str, object] = {
        "id": run_id,
        "role": "subagent",
        "content": description,
        "createdAt": now,
        "meta": {
            "agentName": agent_name,
            "input": description,
            "result": "",
            "status": "running",
            "toolCallId": parent_tool_call_id,
            "subRunId": run_id,
            "runId": run_id,
            "parentRunId": parent_run_id,
            "graphTaskId": graph_task_id,
        },
    }
    _messages(snapshot).append(message)
    task = _find_message(snapshot, parent_tool_call_id)
    task_meta = task.get("meta") if task is not None else None
    if isinstance(task_meta, dict):
        task_meta["subRunId"] = run_id
        task_meta["graphTaskId"] = graph_task_id
        task_meta["agentName"] = agent_name
        task_meta["input"] = description
    return message


def _project_raw_subagents(
    snapshot: dict[str, object], payload: dict[str, object], now: str
) -> None:
    if payload.get("source") != "langgraph.tasks":
        return
    raw_event = payload.get("rawEvent")
    wrapped = payload.get("event")
    if (
        not isinstance(raw_event, dict)
        or raw_event.get("type") != "tasks"
        or raw_event.get("phase") != "start"
        or not isinstance(wrapped, dict)
    ):
        return
    provenance = wrapped.get("provenance")
    descriptors = provenance.get("subagents") if isinstance(provenance, dict) else None
    if not isinstance(descriptors, list):
        return
    runs = cast(dict[str, dict[str, object]], snapshot["runs"])
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        run_id = descriptor.get("runId")
        parent_run_id = descriptor.get("parentAgentRunId")
        agent_name = descriptor.get("agentName")
        graph_task_id = descriptor.get("graphTaskId")
        parent_tool_call_id = descriptor.get("parentToolCallId")
        description = descriptor.get("description")
        if not all(
            isinstance(value, str) and value
            for value in (
                run_id,
                parent_run_id,
                agent_name,
                graph_task_id,
                parent_tool_call_id,
                description,
            )
        ):
            continue
        run_id = cast(str, run_id)
        parent_run_id = cast(str, parent_run_id)
        agent_name = cast(str, agent_name)
        graph_task_id = cast(str, graph_task_id)
        parent_tool_call_id = cast(str, parent_tool_call_id)
        description = cast(str, description)
        runs.setdefault(
            run_id,
            {
                "runId": run_id,
                "status": "running",
                "parentRunId": parent_run_id,
                "agentType": "subagent",
                "agentName": agent_name,
                "graphTaskId": graph_task_id,
                "startedAt": now,
                "completedAt": None,
            },
        )
        _ensure_subagent(
            snapshot,
            run_id=run_id,
            parent_run_id=parent_run_id,
            agent_name=agent_name,
            graph_task_id=graph_task_id,
            parent_tool_call_id=parent_tool_call_id,
            description=description,
            now=now,
        )


def _source(raw_event: object) -> dict[str, object]:
    if not isinstance(raw_event, dict):
        return {"agentType": "main", "agentName": "main"}
    source = raw_event.get("source")
    return (
        source
        if isinstance(source, dict)
        else {"agentType": "main", "agentName": "main"}
    )


def _tool_name(interrupt: dict[str, object]) -> str:
    metadata = interrupt.get("metadata")
    if not isinstance(metadata, dict):
        return "tool"
    action = metadata.get("action_request")
    if not isinstance(action, dict):
        return "tool"
    name = action.get("name")
    return name if isinstance(name, str) else "tool"


def _tool_args(interrupt: dict[str, object]) -> dict[str, object]:
    metadata = interrupt.get("metadata")
    action = metadata.get("action_request") if isinstance(metadata, dict) else None
    args = action.get("args") if isinstance(action, dict) else None
    return args if isinstance(args, dict) else {}


def _allowed_decisions(interrupt: dict[str, object]) -> list[str]:
    metadata = interrupt.get("metadata")
    review = metadata.get("review_config") if isinstance(metadata, dict) else None
    decisions = review.get("allowed_decisions") if isinstance(review, dict) else None
    if not isinstance(decisions, list):
        return []
    return [value for value in decisions if isinstance(value, str)]


def _project_todos(snapshot: dict[str, object], state: dict[str, object]) -> None:
    todos = state.get("todos")
    if not isinstance(todos, list):
        return
    converted: list[dict[str, object]] = []
    for index, todo in enumerate(todos):
        if not isinstance(todo, dict):
            continue
        status = todo.get("status", "pending")
        converted.append(
            {
                "id": str(todo.get("id", f"todo-{index}")),
                "content": str(todo.get("content", "")),
                "status": "running" if status == "in_progress" else status,
            }
        )
    snapshot["todos"] = converted


def reduce_snapshot(
    current: dict[str, object] | None,
    *,
    seq: int,
    event: BaseEvent,
    run_id: str,
    created_at: datetime,
    run_input: dict[str, object] | None,
) -> dict[str, object]:
    """按一个已提交事件生成下一份可信快照"""

    snapshot = deepcopy(current) if current is not None else empty_snapshot()
    payload = cast(
        dict[str, object],
        event.model_dump(mode="json", by_alias=True, exclude_none=True),
    )
    event_type = str(payload["type"])
    raw_event = payload.get("rawEvent")
    source = _source(raw_event)
    source_agent_type = source.get("agentType", "main")
    source_agent_name = source.get("agentName", "main")
    now = created_at.isoformat()

    if event_type == "RAW":
        _project_raw_subagents(snapshot, payload, now)
    elif event_type == "RUN_STARTED":
        event_run_id = str(payload.get("runId", run_id))
        runs = cast(dict[str, dict[str, object]], snapshot["runs"])
        runs[event_run_id] = {
            "runId": event_run_id,
            "status": "running",
            "parentRunId": payload.get("parentRunId"),
            "agentType": source_agent_type,
            "agentName": source_agent_name,
            "graphTaskId": source.get("graphTaskId"),
            "startedAt": now,
            "completedAt": None,
        }
        if source_agent_type == "main":
            snapshot["runStatus"] = "streaming"
            snapshot["activeRunId"] = event_run_id
            if run_input is not None:
                messages = run_input.get("messages")
                if isinstance(messages, list):
                    for item in messages:
                        if not isinstance(item, dict) or item.get("role") != "user":
                            continue
                        message_id = item.get("id")
                        content = item.get("content")
                        if (
                            isinstance(message_id, str)
                            and isinstance(content, str)
                            and _find_message(snapshot, message_id) is None
                        ):
                            _messages(snapshot).append(
                                {
                                    "id": message_id,
                                    "role": "user",
                                    "content": content,
                                    "createdAt": now,
                                }
                            )

    elif event_type == "TEXT_MESSAGE_START":
        child_run_id = raw_event.get("runId") if isinstance(raw_event, dict) else None
        if (
            source_agent_type == "subagent"
            and isinstance(child_run_id, str)
            and _find_subagent(snapshot, child_run_id) is not None
        ):
            return _finish_snapshot(snapshot, seq)
        message_id = str(payload["messageId"])
        if _find_message(snapshot, message_id) is None:
            role = "subagent" if source_agent_type == "subagent" else "assistant"
            _messages(snapshot).append(
                {
                    "id": message_id,
                    "role": role,
                    "content": "",
                    "createdAt": now,
                    "meta": {
                        "status": "running",
                        "agentName": source_agent_name,
                        "runId": run_id,
                        "subRunId": (
                            run_id if source_agent_type == "subagent" else None
                        ),
                        "parentRunId": (
                            raw_event.get("parentAgentRunId")
                            if isinstance(raw_event, dict)
                            else None
                        ),
                        "graphTaskId": source.get("graphTaskId"),
                        "input": source.get("subagentInput"),
                    },
                }
            )
    elif event_type == "TEXT_MESSAGE_CONTENT":
        if source_agent_type == "subagent" and isinstance(raw_event, dict):
            child_run_id = raw_event.get("runId")
            message = (
                _find_subagent(snapshot, child_run_id)
                if isinstance(child_run_id, str)
                else None
            )
            meta = message.get("meta") if message is not None else None
            if isinstance(meta, dict):
                meta["result"] = str(meta.get("result", "")) + str(
                    payload.get("delta", "")
                )
        else:
            message = _find_message(snapshot, str(payload["messageId"]))
            if message is not None:
                message["content"] = str(message.get("content", "")) + str(
                    payload.get("delta", "")
                )
    elif event_type == "TEXT_MESSAGE_END":
        if source_agent_type == "subagent":
            return _finish_snapshot(snapshot, seq)
        message = _find_message(snapshot, str(payload["messageId"]))
        if message is not None:
            meta = message.setdefault("meta", {})
            if isinstance(meta, dict):
                meta["status"] = "completed"
                meta["completedAt"] = now
    elif event_type == "TOOL_CALL_START":
        tool_id = str(payload["toolCallId"])
        if _find_message(snapshot, tool_id) is None:
            _messages(snapshot).append(
                {
                    "id": tool_id,
                    "role": "tool",
                    "content": "",
                    "createdAt": now,
                    "meta": {
                        "title": str(payload.get("toolCallName", "tool")),
                        "toolName": str(payload.get("toolCallName", "tool")),
                        "params": "",
                        "input": "",
                        "status": "running",
                        "toolCallId": tool_id,
                        "runId": run_id,
                        "agentName": source_agent_name,
                        "sourceAgentName": (
                            source_agent_name
                            if source_agent_type == "subagent"
                            else None
                        ),
                        "graphTaskId": source.get("graphTaskId"),
                    },
                }
            )
    elif event_type == "TOOL_CALL_ARGS":
        message = _find_message(snapshot, str(payload["toolCallId"]))
        meta = message.get("meta") if message is not None else None
        if isinstance(meta, dict):
            args = str(meta.get("params", "")) + str(payload.get("delta", ""))
            meta["params"] = args
            meta["input"] = args
    elif event_type == "TOOL_CALL_RESULT":
        tool_result_status = (
            raw_event.get("toolResultStatus") if isinstance(raw_event, dict) else None
        )
        message = _find_message(snapshot, str(payload["toolCallId"]))
        meta = message.get("meta") if message is not None else None
        if isinstance(message, dict) and isinstance(meta, dict):
            result = str(payload.get("content", ""))
            message["content"] = result
            meta["result"] = result
            meta["status"] = "failed" if tool_result_status == "error" else "completed"
            meta["completedAt"] = now
        related_run_id = (
            raw_event.get("relatedRunId") if isinstance(raw_event, dict) else None
        )
        related_result_status = tool_result_status
        if isinstance(related_run_id, str):
            subagent = _find_subagent(snapshot, related_run_id)
            subagent_meta = subagent.get("meta") if subagent is not None else None
            if isinstance(subagent_meta, dict):
                subagent_meta["result"] = str(payload.get("content", ""))
                subagent_meta["status"] = (
                    "failed" if related_result_status == "error" else "completed"
                )
                subagent_meta["completedAt"] = now
            runs = cast(dict[str, dict[str, object]], snapshot["runs"])
            related_run = runs.get(related_run_id)
            if related_run is not None:
                related_run["status"] = (
                    "error" if related_result_status == "error" else "success"
                )
                related_run["completedAt"] = now
    elif event_type == "STATE_SNAPSHOT":
        state = payload.get("snapshot")
        if isinstance(state, dict):
            snapshot["serverState"] = state
            _project_todos(snapshot, state)
    elif event_type == "STATE_DELTA":
        operations = payload.get("delta")
        server_state = snapshot.get("serverState")
        if isinstance(operations, list) and isinstance(server_state, dict):
            patched = jsonpatch.JsonPatch(operations).apply(
                server_state,
                in_place=False,
            )
            if not isinstance(patched, dict):
                raise TypeError("STATE_DELTA 必须保留 object 根状态")
            snapshot["serverState"] = patched
            _project_todos(snapshot, patched)
    elif event_type == "RUN_FINISHED":
        event_run_id = str(payload.get("runId", run_id))
        outcome = payload.get("outcome")
        outcome_type = (
            outcome.get("type", "success") if isinstance(outcome, dict) else "success"
        )
        runs = cast(dict[str, dict[str, object]], snapshot["runs"])
        if event_run_id in runs:
            runs[event_run_id]["status"] = outcome_type
            runs[event_run_id]["completedAt"] = now
        if source_agent_type == "main":
            snapshot["activeRunId"] = None
            if outcome_type == "interrupt" and isinstance(outcome, dict):
                interrupts = outcome.get("interrupts")
                public_interrupts = (
                    [item for item in interrupts if isinstance(item, dict)]
                    if isinstance(interrupts, list)
                    else []
                )
                items = [
                    {
                        "id": str(item.get("id", "")),
                        "interruptId": str(item.get("id", "")),
                        "toolCallId": item.get("toolCallId"),
                        "toolName": _tool_name(item),
                        "params": str(_tool_args(item)),
                        "input": str(_tool_args(item)),
                        "description": str(item.get("message", "")),
                        "originalArgs": _tool_args(item),
                        "allowedDecisions": _allowed_decisions(item),
                    }
                    for item in public_interrupts
                ]
                snapshot["approval"] = {
                    "items": items,
                    "activeIndex": 0,
                    "submitted": False,
                }
                snapshot["interrupts"] = public_interrupts
                snapshot["runStatus"] = "waiting_approval"
            else:
                snapshot["approval"] = None
                snapshot["interrupts"] = []
                snapshot["runStatus"] = "idle"
    elif event_type == "RUN_ERROR":
        runs = cast(dict[str, dict[str, object]], snapshot["runs"])
        error_message = str(payload.get("message", "对话运行失败"))
        event_run_id = str(
            payload.get("runId")
            or (raw_event.get("runId") if isinstance(raw_event, dict) else run_id)
        )
        terminal_status = (
            "cancelled"
            if payload.get("code") in {"cancelled", "resume_cancelled"}
            else "error"
        )
        if event_run_id in runs:
            runs[event_run_id]["status"] = terminal_status
            runs[event_run_id]["completedAt"] = now
        if source_agent_type == "subagent":
            for message in _messages(snapshot):
                meta = message.get("meta")
                if (
                    message.get("role") == "subagent"
                    and isinstance(meta, dict)
                    and meta.get("runId") == event_run_id
                ):
                    meta["status"] = "failed"
                    meta["completedAt"] = now
        else:
            snapshot["activeRunId"] = None
            snapshot["runStatus"] = (
                "idle" if terminal_status == "cancelled" else "error"
            )
            for run in runs.values():
                if (
                    run.get("agentType") == "subagent"
                    and run.get("status") == "running"
                ):
                    run["status"] = terminal_status
                    run["completedAt"] = now
            for message in _messages(snapshot):
                meta = message.get("meta")
                if (
                    message.get("role") in {"tool", "subagent"}
                    and isinstance(meta, dict)
                    and meta.get("status") in {"running", "paused"}
                ):
                    meta["status"] = "failed"
                    if message.get("role") == "tool" or not meta.get("result"):
                        meta["result"] = error_message
                    meta["completedAt"] = now
            _messages(snapshot).append(
                {
                    "id": f"error-{seq}",
                    "role": "error",
                    "content": error_message,
                    "createdAt": now,
                }
            )

    return _finish_snapshot(snapshot, seq)


def _finish_snapshot(snapshot: dict[str, object], seq: int) -> dict[str, object]:
    snapshot["snapshotSeq"] = seq
    snapshot["snapshotVersion"] = 2
    return snapshot
