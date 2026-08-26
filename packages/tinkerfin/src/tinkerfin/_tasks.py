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
        cancel: Whether to request task cancellation before joining.
        suppress_task_cancellation: Whether owned task cancellation is expected.

    Returns:
        The owned task result, or ``None`` for an expected task cancellation.

    Raises:
        asyncio.CancelledError: The caller or owned task is cancelled.
        BaseException: The owned task fails and no caller cancellation outranks it.
    """

    if cancel and not task.done():
        task.cancel()
    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            next_cancel_count = current.cancelling() if current is not None else 0
            if next_cancel_count > cancel_count:
                if caller_cancellation is None:
                    caller_cancellation = error
                cancel_count = next_cancel_count
                continue
            if task.done():
                break
            raise

    task_error: BaseException | None = None
    result: _TaskResult | None = None
    try:
        result = task.result()
    except asyncio.CancelledError as error:
        if not suppress_task_cancellation:
            task_error = error
    except BaseException as error:  # noqa: BLE001 - preserve the owned task outcome
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
