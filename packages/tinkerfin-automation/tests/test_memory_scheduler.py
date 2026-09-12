from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.scheduler import AutomationScheduler, MemoryScheduler


def test_memory_scheduler_conforms_to_public_scheduler_protocol() -> None:
    scheduler: AutomationScheduler = MemoryScheduler(
        clock=ManualClock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    )
    assert scheduler is not None


@pytest.mark.asyncio
async def test_scheduler_replaces_removes_and_dispatches_due_wakeups() -> None:
    now = datetime(2026, 9, 9, 8, tzinfo=UTC)
    clock = ManualClock(now)
    scheduler = MemoryScheduler(clock=clock)
    delivered: list[str] = []

    async def on_task_due(task_id: str) -> None:
        delivered.append(task_id)

    await scheduler.start(on_task_due)
    try:
        await scheduler.schedule_task("removed", now)
        await scheduler.remove_task("removed")
        await scheduler.schedule_task("task-1", now + timedelta(hours=1))
        await scheduler.schedule_task("task-1", now)
        await scheduler.dispatch_due()
        await scheduler.dispatch_due()
        assert delivered == ["task-1"]
    finally:
        await scheduler.close()
        await scheduler.close()
