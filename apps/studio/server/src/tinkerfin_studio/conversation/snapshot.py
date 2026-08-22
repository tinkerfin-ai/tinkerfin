"""AG-UI 事件到前端 v3 快照的纯数据归约"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import cast

import jsonpatch
from ag_ui.core import BaseEvent
from ag_ui.core import Interrupt as AgUiInterrupt

from tinkerfin_agui_adapter import SubagentProvenance, parse_tool_review_interrupt


def empty_snapshot() -> dict[str, object]:
    """创建一个可直接返回前端的空 v3 快照"""

    return {
        "snapshotSeq": 0,
        "snapshotVersion": 3,
        "messages": [],
        "todos": [],
        "mode": "default",
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
    origin_main_run_id: str,
    last_main_run_id: str,
    agent_name: str,
    graph_task_id: str,
    parent_tool_call_id: str,
    description: str,
    now: str,
) -> dict[str, object]:
    existing = _find_subagent(snapshot, run_id)
    if existing is not None:
        meta = existing.get("meta")
        if not isinstance(meta, dict):
            raise TypeError("子 Agent 卡片缺少 meta")
        if (
            meta.get("originMainRunId") != origin_main_run_id
            or meta.get("agentName") != agent_name
            or meta.get("graphTaskId") != graph_task_id
            or meta.get("toolCallId") != parent_tool_call_id
            or meta.get("input") != description
        ):
            raise ValueError(f"子 Agent 快照身份冲突: {run_id}")
        meta["lastMainRunId"] = last_main_run_id
        meta["status"] = "running"
        meta["completedAt"] = None
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
            "originMainRunId": origin_main_run_id,
            "lastMainRunId": last_main_run_id,
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
            raise TypeError("RAW task subagents 项必须是对象")
        provenance = SubagentProvenance.model_validate(descriptor)
        run_id = provenance.subagent_invocation_id
        existing_run = runs.get(run_id)
        if existing_run is None:
            runs[run_id] = {
                "runId": provenance.subagent_invocation_id,
                "status": "running",
                "originMainRunId": provenance.request_run_id,
                "lastMainRunId": provenance.request_run_id,
                "agentType": "subagent",
                "agentName": provenance.agent_name,
                "graphTaskId": provenance.graph_task_id,
                "namespace": list(provenance.namespace),
                "parentToolCallId": provenance.parent_tool_call_id,
                "description": provenance.description,
                "startedAt": now,
                "completedAt": None,
            }
        else:
            stable = {
                "agentName": existing_run.get("agentName"),
                "graphTaskId": existing_run.get("graphTaskId"),
                "namespace": existing_run.get("namespace"),
                "parentToolCallId": existing_run.get("parentToolCallId"),
                "description": existing_run.get("description"),
            }
            expected = {
                "agentName": provenance.agent_name,
                "graphTaskId": provenance.graph_task_id,
                "namespace": list(provenance.namespace),
                "parentToolCallId": provenance.parent_tool_call_id,
                "description": provenance.description,
            }
            if stable != expected:
                raise ValueError(f"子 Agent 快照身份冲突: {run_id}")
            existing_run["lastMainRunId"] = provenance.request_run_id
            existing_run["status"] = "running"
            existing_run["completedAt"] = None
        _ensure_subagent(
            snapshot,
            run_id=run_id,
            origin_main_run_id=cast(str, runs[run_id]["originMainRunId"]),
            last_main_run_id=provenance.request_run_id,
            agent_name=provenance.agent_name,
            graph_task_id=provenance.graph_task_id,
            parent_tool_call_id=provenance.parent_tool_call_id,
            description=provenance.description,
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


def _approval_item(interrupt: dict[str, object]) -> dict[str, object]:
    """把一个公开 Tool interrupt 转为可刷新恢复的审批项"""

    public_interrupt = AgUiInterrupt.model_validate(interrupt)
    review = parse_tool_review_interrupt(public_interrupt)
    interrupt_id = public_interrupt.id
    tool_call_id = public_interrupt.tool_call_id
    assert tool_call_id is not None
    args = review.original_args.root
    params = json.dumps(args, ensure_ascii=False, indent=2)
    return {
        "id": interrupt_id,
        "interruptId": interrupt_id,
        "toolCallId": tool_call_id,
        "toolName": review.tool_name,
        "params": params,
        "input": params,
        "description": public_interrupt.message or "",
        "originalArgs": args,
        "allowedDecisions": list(review.allowed_decisions),
    }


def _project_pending_interrupts(
    snapshot: dict[str, object],
    interrupts: list[dict[str, object]],
) -> None:
    """用当前完整 pending 组更新快照中的交互投影"""

    projected = deepcopy(interrupts)
    snapshot["interrupts"] = projected
    if not projected:
        snapshot["approval"] = None
        return
    reasons = {item.get("reason") for item in projected}
    plan_reasons = {"plan_clarification", "plan_review"}
    if reasons <= plan_reasons:
        snapshot["approval"] = None
        return
    if reasons != {"tool_call"}:
        raise ValueError("pending interrupt 不能混合 Plan、Tool 或未知 reason")
    snapshot["approval"] = {
        "items": [_approval_item(item) for item in projected],
        "activeIndex": 0,
        "submitted": False,
    }


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
    resume = run_input.get("resume") if run_input is not None else None
    initialization_failed = (
        isinstance(raw_event, dict) and raw_event.get("initializationFailed") is True
    )
    preserve_pending_after_initialization_failure = (
        initialization_failed
        and isinstance(resume, list)
        and bool(resume)
        and isinstance(snapshot.get("interrupts"), list)
        and bool(snapshot["interrupts"])
    )

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
            if not preserve_pending_after_initialization_failure:
                snapshot["runStatus"] = "streaming"
                snapshot["activeRunId"] = event_run_id
            if run_input is not None:
                if (
                    not preserve_pending_after_initialization_failure
                    and isinstance(resume, list)
                    and resume
                ):
                    # Run 注册要求 resume 完整覆盖待处理组；RUN_STARTED 持久化后，
                    # 即使 resumed run 随后失败或取消，原 interrupt 也不再 pending
                    snapshot["approval"] = None
                    snapshot["interrupts"] = []
                forwarded_props = run_input.get("forwardedProps")
                mode = (
                    forwarded_props.get("mode")
                    if isinstance(forwarded_props, dict)
                    else None
                )
                if mode in {"default", "plan"}:
                    snapshot["mode"] = mode
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
        child_run_id = source.get("subagentInvocationId")
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
                        "runId": (
                            child_run_id if source_agent_type == "subagent" else run_id
                        ),
                        "subRunId": (
                            child_run_id if source_agent_type == "subagent" else None
                        ),
                        "graphTaskId": source.get("graphTaskId"),
                        "input": source.get("subagentInput"),
                    },
                }
            )
    elif event_type == "TEXT_MESSAGE_CONTENT":
        if source_agent_type == "subagent":
            child_run_id = source.get("subagentInvocationId")
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
                        "runId": (
                            source.get("subagentInvocationId")
                            if source_agent_type == "subagent"
                            else run_id
                        ),
                        "subRunId": (
                            source.get("subagentInvocationId")
                            if source_agent_type == "subagent"
                            else None
                        ),
                        "lastMainRunId": (
                            raw_event.get("runId")
                            if source_agent_type == "subagent"
                            and isinstance(raw_event, dict)
                            else None
                        ),
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
            raw_event.get("relatedSubagentInvocationId")
            if isinstance(raw_event, dict)
            else None
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
                _project_pending_interrupts(snapshot, public_interrupts)
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
        snapshot_run_id = (
            str(source.get("subagentInvocationId"))
            if source_agent_type == "subagent"
            and isinstance(source.get("subagentInvocationId"), str)
            else event_run_id
        )
        terminal_status = (
            "cancelled"
            if payload.get("code") in {"cancelled", "resume_cancelled"}
            else "error"
        )
        if snapshot_run_id in runs:
            runs[snapshot_run_id]["status"] = terminal_status
            runs[snapshot_run_id]["completedAt"] = now
        if source_agent_type == "subagent":
            for message in _messages(snapshot):
                meta = message.get("meta")
                if (
                    message.get("role") == "subagent"
                    and isinstance(meta, dict)
                    and meta.get("runId") == snapshot_run_id
                ):
                    meta["status"] = "failed"
                    meta["completedAt"] = now
        else:
            snapshot["activeRunId"] = None
            if preserve_pending_after_initialization_failure:
                snapshot["runStatus"] = "waiting_approval"
            else:
                snapshot["runStatus"] = (
                    "idle" if terminal_status == "cancelled" else "error"
                )
                for run in runs.values():
                    if (
                        run.get("agentType") == "subagent"
                        and run.get("status") == "running"
                        and run.get("lastMainRunId") == event_run_id
                    ):
                        run["status"] = terminal_status
                        run["completedAt"] = now
                for message in _messages(snapshot):
                    meta = message.get("meta")
                    if (
                        message.get("role") in {"tool", "subagent"}
                        and isinstance(meta, dict)
                        and meta.get("status") in {"running", "paused"}
                        and (
                            (
                                message.get("role") == "tool"
                                and meta.get("subRunId") is None
                            )
                            or meta.get("lastMainRunId") == event_run_id
                        )
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
    snapshot["snapshotVersion"] = 3
    return snapshot
