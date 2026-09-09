"""Cancellation-safe joins for runtime-owned asynchronous tasks."""

from __future__ import annotations

import asyncio
from typing import TypeVar

__all__ = ["join_task"]

_TaskResult = TypeVar("_TaskResult")


async def join_task(
    task: asyncio.Task[_TaskResult],
    *,
    cancel: bool = False,
    suppress_task_cancellation: bool = False,
) -> _TaskResult | None:
    """Join an owned task without losing cancellation of the joining caller.

    Args:
        task: Runtime-owned task that must settle before this call returns.
        cancel: Whether to request cancellation if no cancellation is already pending.
        suppress_task_cancellation: Whether owned task cancellation is expected.

    Returns:
        The owned task result, or ``None`` for an expected task cancellation.

    Raises:
        asyncio.CancelledError: The caller or owned task is cancelled.
        Exception: The owned task fails without caller cancellation.
        BaseException: The owned task raises a process-control exception, which
            propagates unchanged even if the caller has requested cancellation.
    """

    if cancel and not task.done() and not task.cancelling():
        task.cancel()
    caller_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            # wait() neither cancels the owned task nor propagates its outcome.
            # Every cancellation here therefore belongs to the joining caller;
            # ordinary failure and owned cancellation are read once below. Always
            # await once so a pending caller cancellation is delivered even when
            # the owned task has already completed.
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            if caller_cancellation is None:
                caller_cancellation = error
            if not task.done():
                continue
        break

    task_error: Exception | asyncio.CancelledError | None = None
    result: _TaskResult | None = None
    try:
        result = task.result()
    except asyncio.CancelledError as error:
        if not suppress_task_cancellation:
            task_error = error
    except Exception as error:  # noqa: BLE001 - retain failure behind caller cancellation
        task_error = error

    if caller_cancellation is not None:
        if task_error is not None:
            caller_cancellation.add_note(
                f"owned task also failed: {type(task_error).__name__}: {task_error}"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)
    return result
