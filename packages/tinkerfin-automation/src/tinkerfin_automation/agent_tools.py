"""Host-bound LangChain tools for managing Automation tasks and executions."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ._rules import require_task_revision
from .models import AutomationExecution, AutomationTask
from .schedules import Schedule
from .service import AutomationService


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CreateAutomationInput(_ToolInput):
    name: str = Field(min_length=1, max_length=255)
    schedule: Schedule
    target: str = Field(
        min_length=1,
        max_length=191,
        description="Registered target allowed by the host",
    )
    input: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Secret-free JSON input passed to each execution",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable idempotency key reused only for this exact request",
    )


class _UpdateAutomationInput(_ToolInput):
    task_id: str = Field(description="Task identity returned by an Automation tool")
    expected_revision: int = Field(
        ge=1,
        description="Current task revision required for conflict detection",
    )
    name: str | None = Field(default=None, min_length=1, max_length=255)
    schedule: Schedule | None = None
    target: str | None = Field(default=None, min_length=1, max_length=191)
    input: dict[str, JsonValue] | None = None
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable idempotency key reused only for this exact request",
    )


class _TaskMutationInput(_ToolInput):
    task_id: str = Field(description="Task identity returned by an Automation tool")
    expected_revision: int = Field(
        ge=1,
        description="Current task revision required for conflict detection",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable idempotency key reused only for this exact request",
    )


class _TaskLookupInput(_ToolInput):
    task_id: str = Field(description="Task identity returned by an Automation tool")


class _ListAutomationsInput(_ToolInput):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(
        default=None,
        description="Opaque next cursor returned by the preceding list call",
    )


class _RunAutomationTaskNowInput(_ToolInput):
    task_id: str = Field(description="Task identity returned by an Automation tool")
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable idempotency key reused only for this exact request",
    )


class _ExecuteAutomationOnceInput(_ToolInput):
    target: str = Field(
        min_length=1,
        max_length=191,
        description="Registered target allowed by the host",
    )
    input: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Secret-free JSON input passed to this execution",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable idempotency key reused only for this exact request",
    )


def _task_output(task: AutomationTask) -> dict[str, JsonValue]:
    return {
        "task_id": task.task_id,
        "name": task.name,
        "target": task.target,
        "input": dict(task.input),
        "schedule": task.schedule.model_dump(mode="json"),
        "status": task.status.value,
        "revision": task.revision,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
    }


def _execution_output(execution: AutomationExecution) -> dict[str, JsonValue]:
    return {
        "execution_id": execution.execution_id,
        "task_id": execution.task_id,
        "status": execution.status.value,
        "attempt": execution.attempt,
        "queued_at": execution.queued_at.isoformat(),
    }


def create_automation_tools(
    service: AutomationService,
    *,
    owner_id: str,
    allowed_targets: Collection[str],
) -> tuple[BaseTool, ...]:
    """Create Automation tools bound to trusted identity and target permissions.

    Args:
        service: Shared service used by non-Agent callers as well.
        owner_id: Trusted subject identity captured outside model input.
        allowed_targets: Target names this Agent may schedule or execute.

    Returns:
        Nine LangChain tools with the supplied owner and target permissions.

    Raises:
        ValueError: No allowed target is supplied.
    """

    allowed = frozenset(allowed_targets)
    if not allowed:
        raise ValueError("allowed_targets must not be empty")

    def require_target(target: str) -> None:
        if target not in allowed:
            raise ValueError("target is not allowed for this Agent")

    async def authorized_task(
        task_id: str, *, expected_revision: int | None = None, target: str | None = None
    ) -> AutomationTask:
        task = await service.get_task(owner_id=owner_id, task_id=task_id)
        # Older revisions can be idempotent retries; the Store decides those before
        # its atomic revision check. A future revision cannot authorize a target we
        # have not observed, even if a concurrent edit later creates that revision.
        if expected_revision is not None and expected_revision > task.revision:
            require_task_revision(task, expected_revision)
        require_target(task.target if target is None else target)
        return task

    async def create_automation(
        name: str,
        schedule: Schedule,
        target: str,
        input: Mapping[str, JsonValue],
        request_id: str,
    ) -> dict[str, JsonValue]:
        require_target(target)
        return _task_output(
            await service.create_task(
                owner_id=owner_id,
                name=name,
                schedule=schedule,
                target=target,
                input=input,
                request_id=request_id,
            )
        )

    async def update_automation(
        task_id: str,
        expected_revision: int,
        name: str | None,
        schedule: Schedule | None,
        target: str | None,
        input: Mapping[str, JsonValue] | None,
        request_id: str,
    ) -> dict[str, JsonValue]:
        await authorized_task(
            task_id, expected_revision=expected_revision, target=target
        )
        return _task_output(
            await service.update_task(
                owner_id=owner_id,
                task_id=task_id,
                expected_revision=expected_revision,
                name=name,
                schedule=schedule,
                target=target,
                input=input,
                request_id=request_id,
            )
        )

    async def pause_automation(
        task_id: str, expected_revision: int, request_id: str
    ) -> dict[str, JsonValue]:
        return _task_output(
            await service.pause_task(
                owner_id=owner_id,
                task_id=task_id,
                expected_revision=expected_revision,
                request_id=request_id,
            )
        )

    async def enable_automation(
        task_id: str, expected_revision: int, request_id: str
    ) -> dict[str, JsonValue]:
        await authorized_task(task_id, expected_revision=expected_revision)
        return _task_output(
            await service.enable_task(
                owner_id=owner_id,
                task_id=task_id,
                expected_revision=expected_revision,
                request_id=request_id,
            )
        )

    async def delete_automation(
        task_id: str, expected_revision: int, request_id: str
    ) -> dict[str, JsonValue]:
        await service.delete_task(
            owner_id=owner_id,
            task_id=task_id,
            expected_revision=expected_revision,
            request_id=request_id,
        )
        return {"task_id": task_id, "deleted": True}

    async def get_automation(task_id: str) -> dict[str, JsonValue]:
        return _task_output(await service.get_task(owner_id=owner_id, task_id=task_id))

    async def list_automations(
        limit: int = 50, cursor: str | None = None
    ) -> dict[str, JsonValue]:
        page = await service.list_tasks(owner_id=owner_id, limit=limit, cursor=cursor)
        return {
            "items": [_task_output(task) for task in page.items],
            "next_cursor": page.next_cursor,
        }

    async def execute_automation_once(
        target: str,
        input: Mapping[str, JsonValue],
        request_id: str,
    ) -> dict[str, JsonValue]:
        require_target(target)
        return _execution_output(
            await service.execute_once(
                owner_id=owner_id,
                target=target,
                input=input,
                request_id=request_id,
            )
        )

    async def run_automation_task_now(
        task_id: str, request_id: str
    ) -> dict[str, JsonValue]:
        task = await authorized_task(task_id)
        return _execution_output(
            await service.run_task_now(
                owner_id=owner_id,
                task_id=task_id,
                request_id=request_id,
                expected_revision=task.revision,
            )
        )

    definitions = (
        (
            create_automation,
            "create_automation",
            "Create a scheduled task for the current user.",
            _CreateAutomationInput,
        ),
        (
            update_automation,
            "update_automation",
            "Update selected fields of an existing task.",
            _UpdateAutomationInput,
        ),
        (
            pause_automation,
            "pause_automation",
            "Pause future runs without cancelling existing executions.",
            _TaskMutationInput,
        ),
        (
            enable_automation,
            "enable_automation",
            "Enable future runs from the current time.",
            _TaskMutationInput,
        ),
        (
            delete_automation,
            "delete_automation",
            "Delete a task while preserving execution history.",
            _TaskMutationInput,
        ),
        (
            get_automation,
            "get_automation",
            "Get one task owned by the current user.",
            _TaskLookupInput,
        ),
        (
            list_automations,
            "list_automations",
            "List tasks owned by the current user.",
            _ListAutomationsInput,
        ),
        (
            execute_automation_once,
            "execute_automation_once",
            "Execute an allowed target once now without creating a reusable task.",
            _ExecuteAutomationOnceInput,
        ),
        (
            run_automation_task_now,
            "run_automation_task_now",
            "Run an existing task once now without changing its saved schedule.",
            _RunAutomationTaskNowInput,
        ),
    )
    return tuple(
        StructuredTool.from_function(
            coroutine=coroutine,
            name=name,
            description=description,
            args_schema=args_schema,
        )
        for coroutine, name, description, args_schema in definitions
    )


__all__ = ["create_automation_tools"]
