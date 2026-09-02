"""Materialize query entries from index metadata and referenced Ledger facts."""

from __future__ import annotations

from pydantic import JsonValue

from ._ids import scope_id
from .capture import CapturedValue
from .entries import TraceEntry, TraceEntryKind, TraceFailure
from .errors import TraceStoreProtocolError
from .facts import (
    AgentStepFact,
    ContextContributionFact,
    MiddlewareFact,
    ModelCallFact,
    RunFact,
    RuntimeTaskFact,
    SkillFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
)
from .store import TraceEntryRecord


def _captured(value: CapturedValue | None) -> tuple[JsonValue | None, bool]:
    if value is None:
        return None, False
    return value.value, value.disposition == "omitted"


def _failure(
    error_type: str,
    *,
    message: CapturedValue | None = None,
    code: str | None = None,
) -> TraceFailure:
    value, _omitted = _captured(message)
    return TraceFailure(
        error_type=error_type,
        message=value if isinstance(value, str) else None,
        code=code,
    )


def project_trace_entry(record: TraceEntryRecord, *, turn_id: str) -> TraceEntry:
    """Build one public entry without trusting duplicated index detail fields."""

    start = record.started_event.fact
    update = record.updated_event.fact
    request: JsonValue | None = None
    request_omitted = False
    result: JsonValue | None = None
    result_omitted = False
    usage: JsonValue | None = None
    response_metadata: JsonValue | None = None
    failure: TraceFailure | None = None
    proposal_id: str | None = None
    source_id: str | None = None
    class_name: str | None = None
    hooks: tuple[str, ...] = ()
    source_path: str | None = None

    if record.kind is TraceEntryKind.RUN:
        if not isinstance(start, RunFact) or start.phase != "started":
            raise TraceStoreProtocolError("Run entry start fact is invalid")
        if (
            isinstance(update, RunFact)
            and update.outcome == "failed"
            and update.error_type is not None
            and update.failure_origin
        ):
            failure = _failure(update.error_type, code=update.code)
    elif record.kind in {
        TraceEntryKind.AGENT,
        TraceEntryKind.MODEL,
        TraceEntryKind.TOOLS,
    }:
        if not isinstance(start, AgentStepFact) or start.phase != "started":
            raise TraceStoreProtocolError("Agent step entry start fact is invalid")
        source_id = start.source_task_id
        if (
            isinstance(update, AgentStepFact)
            and update.phase == "failed"
            and update.error_type is not None
            and update.failure_origin
        ):
            failure = _failure(update.error_type, message=update.error_message)
    elif record.kind is TraceEntryKind.PROVIDER:
        if not isinstance(start, ModelCallFact) or start.phase != "started":
            raise TraceStoreProtocolError("Provider entry start fact is invalid")
        request, request_omitted = _captured(start.request)
        if isinstance(update, ModelCallFact):
            usage, _usage_omitted = _captured(update.usage)
            response_metadata, _metadata_omitted = _captured(update.response_metadata)
            if (
                update.phase == "failed"
                and update.error_type is not None
                and update.failure_origin
            ):
                failure = _failure(update.error_type, message=update.error_message)
    elif record.kind is TraceEntryKind.TOOL_PROPOSAL:
        if not isinstance(start, ToolFact) or start.phase not in {
            "started",
            "arguments",
        }:
            raise TraceStoreProtocolError("Tool proposal start fact is invalid")
        source_id = start.source_tool_call_id
        if start.phase == "arguments":
            request, request_omitted = _captured(start.content)
        if isinstance(update, ToolFact) and update.phase == "result":
            result, result_omitted = _captured(update.content)
            if update.result_status == "error" and update.failure_origin:
                failure = TraceFailure(error_type="tool_error")
    elif record.kind is TraceEntryKind.TOOL:
        if not isinstance(start, ToolExecutionFact) or start.phase != "started":
            raise TraceStoreProtocolError("Tool execution start fact is invalid")
        source_id = start.source_tool_call_id
        if start.source_tool_call_id is not None:
            proposal_id = scope_id(
                "tool",
                start.namespace,
                start.source_tool_call_id,
            )
        request, request_omitted = _captured(start.input)
        if isinstance(update, ToolExecutionFact):
            result, result_omitted = _captured(update.output)
            if update.phase == "failed" and update.failure_origin:
                failure = _failure(
                    update.error_type or "tool_error",
                    message=update.error_message,
                )
    elif record.kind is TraceEntryKind.SUBAGENT:
        if not isinstance(start, SubagentFact) or start.phase != "started":
            raise TraceStoreProtocolError("Subagent entry start fact is invalid")
        source_id = start.parent_tool_call_id
        request, request_omitted = _captured(start.input)
    elif record.kind is TraceEntryKind.TASK:
        if isinstance(start, AgentStepFact) and start.phase == "started":
            source_id = start.source_task_id
            if (
                isinstance(update, AgentStepFact)
                and update.phase == "failed"
                and update.error_type is not None
                and update.failure_origin
            ):
                failure = _failure(
                    update.error_type,
                    message=update.error_message,
                )
        elif isinstance(start, RuntimeTaskFact) and start.phase == "started":
            source_id = start.source_task_id
            request, request_omitted = _captured(start.input)
            if isinstance(update, RuntimeTaskFact):
                result, result_omitted = _captured(update.result)
                if (
                    update.phase == "failed"
                    and update.error_type is not None
                    and update.failure_origin
                ):
                    failure = TraceFailure(error_type=update.error_type)
        else:
            raise TraceStoreProtocolError("Runtime task entry start fact is invalid")
    elif record.kind is TraceEntryKind.MIDDLEWARE:
        if isinstance(start, MiddlewareFact):
            class_name = start.class_name
            hooks = start.hooks
        elif (
            isinstance(start, AgentStepFact)
            and start.phase == "started"
            and start.step_kind == "middleware"
        ):
            source_id = start.source_task_id
            hooks = () if start.hook is None else (start.hook,)
            if (
                isinstance(update, AgentStepFact)
                and update.phase == "failed"
                and update.error_type is not None
                and update.failure_origin
            ):
                failure = _failure(
                    update.error_type,
                    message=update.error_message,
                )
        else:
            raise TraceStoreProtocolError("Middleware entry fact is invalid")
    elif record.kind is TraceEntryKind.SKILL:
        if not isinstance(start, SkillFact):
            raise TraceStoreProtocolError("Skill entry fact is invalid")
        source_id = start.execution_id
        source_path = start.source_path
    elif record.kind in {
        TraceEntryKind.MEMORY,
        TraceEntryKind.GUARDRAIL,
        TraceEntryKind.RETRIEVAL,
        TraceEntryKind.CUSTOM,
    }:
        if not isinstance(start, ContextContributionFact) or start.phase != "started":
            raise TraceStoreProtocolError("Context entry start fact is invalid")
        request, request_omitted = _captured(start.input)
        if isinstance(update, ContextContributionFact):
            result, result_omitted = _captured(update.output)
            if (
                update.phase == "failed"
                and update.error_type is not None
                and update.failure_origin
            ):
                failure = TraceFailure(error_type=update.error_type)
    else:
        raise TraceStoreProtocolError("Trace entry kind has no current projection")

    return TraceEntry(
        id=record.entry_id,
        turn_id=turn_id,
        parent_id=record.parent_id,
        proposal_id=proposal_id,
        kind=record.kind,
        status=record.status,
        name=record.name,
        run_id=record.run_id,
        namespace=record.namespace,
        agent_name=record.agent_name,
        provider=record.provider,
        model=record.model,
        source_id=source_id,
        started_at=record.started_at,
        first_output_at=record.first_output_at,
        completed_at=record.completed_at,
        started_seq=record.started_seq,
        updated_seq=record.updated_seq,
        request=request,
        request_omitted=request_omitted,
        result=result,
        result_omitted=result_omitted,
        usage=usage,
        response_metadata=response_metadata,
        failure=failure,
        class_name=class_name,
        hooks=hooks,
        source_path=source_path,
    )


__all__ = ["project_trace_entry"]
