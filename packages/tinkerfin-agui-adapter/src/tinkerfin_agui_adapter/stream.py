"""High-level, backpressured Deep Agents v2 to AG-UI streaming."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from typing import cast

from ag_ui.core import BaseEvent

from .adapter import DeepAgentAgUiAdapter
from .lifecycle import AgUiLifecycleEventFactory
from .microbatch import micro_batch

logger = logging.getLogger(__name__)


async def _close_upstream(
    iterator: AsyncIterator[object],
    primary: BaseException | None,
) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    try:
        await cast(Callable[[], Awaitable[object]], close)()
    except BaseException as error:
        if primary is not None and not isinstance(primary, GeneratorExit):
            primary.add_note(
                f"upstream close also failed: {type(error).__name__}: {error}"
            )
            raise primary.with_traceback(primary.__traceback__)
        raise


async def astream_events(
    parts: AsyncIterable[object],
    *,
    thread_id: str,
    run_id: str,
    parent_run_id: str | None = None,
    expose_reasoning_events: bool = False,
    expose_subagent_events: bool = True,
    prior_tool_call_ids: frozenset[str] = frozenset(),
) -> AsyncIterator[BaseEvent]:
    """Convert a caller-supplied Deep Agents v2 stream into one AG-UI lifecycle.

    Parts are validated one at a time before adapter state changes. The function
    does not prefetch into a queue: while a text batch awaits its 0.3-second
    wall-clock flush, at most one upstream pull remains pending. That single-slot
    lookahead is retained across a timed flush so cancelling it cannot truncate an
    arbitrary async iterator. The function does not create a graph, hold a
    checkpointer, or own transport state. Ordinary conversion failures close open
    child lifecycles and emit one main `RUN_ERROR`. Cancellation, generator closure,
    and consumer abandonment propagate while closing an upstream iterator that
    exposes `aclose()`.

    Output order is `RUN_STARTED`, converted child events, closed child lifecycles,
    and exactly one success/interrupt terminal. On ordinary conversion failure it
    instead closes child lifecycles and emits exactly one `RUN_ERROR`.

    Provider reasoning metadata at `additional_kwargs.reasoning_content` is removed
    from public payloads independently of `expose_reasoning_events`; the flag enables
    `REASONING_*` events from supported reasoning sources and does not grant raw-
    provider visibility.

    Args:
        parts: Live `messages`, `tasks`, `values`, `updates`, `checkpoints`,
            `debug`, and `custom` v2 parts produced under the Deep Agents profile,
            with subgraph provenance supplied by native task starts rather than
            arbitrary LangGraph namespace inference.
        thread_id: AG-UI conversation identity supplied by the transport host.
        run_id: AG-UI main-run identity supplied by the transport host.
        parent_run_id: Optional AG-UI parent-run identity supplied by the host.
        expose_reasoning_events: Emit supported reasoning events when true.
        expose_subagent_events: Emit events derived from non-root namespaces when true.
        prior_tool_call_ids: Native Tool call IDs already emitted before resume.

    Yields:
        Validated AG-UI events in protocol order.
    """

    for name, value in (("thread_id", thread_id), ("run_id", run_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        if not value or value != value.strip():
            raise ValueError(f"{name} must be non-blank without surrounding whitespace")
    if parent_run_id is not None:
        if not isinstance(parent_run_id, str):
            raise TypeError("parent_run_id must be a string or None")
        if not parent_run_id or parent_run_id != parent_run_id.strip():
            raise ValueError(
                "parent_run_id must be non-blank without surrounding whitespace"
            )
    if not isinstance(expose_reasoning_events, bool):
        raise TypeError("expose_reasoning_events must be a bool")
    if not isinstance(expose_subagent_events, bool):
        raise TypeError("expose_subagent_events must be a bool")
    adapter = DeepAgentAgUiAdapter(
        run_id,
        expose_reasoning_events=expose_reasoning_events,
        expose_subagent_events=expose_subagent_events,
        prior_tool_call_ids=prior_tool_call_ids,
    )
    lifecycle = AgUiLifecycleEventFactory()
    upstream = aiter(parts)

    async def converted() -> AsyncIterator[BaseEvent]:
        primary: BaseException | None = None
        started = False
        terminal = False
        upstream_closed = False

        async def close_upstream(primary_error: BaseException | None) -> None:
            nonlocal upstream_closed
            if upstream_closed:
                return
            try:
                await _close_upstream(upstream, primary_error)
            except asyncio.CancelledError:
                # The close coroutine was interrupted, so the protected outer
                # microbatch cleanup must retain permission to finish it.
                raise
            except BaseException:
                upstream_closed = True
                raise
            else:
                upstream_closed = True

        try:
            started = True
            yield lifecycle.started(
                thread_id=thread_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
            )
            async for part in upstream:
                for event in adapter.process(part):
                    yield event
            await close_upstream(None)
            for event in adapter.finish():
                yield event
            terminal = True
            yield lifecycle.finished(
                thread_id=thread_id,
                run_id=run_id,
                outcome=adapter.main_outcome(),
            )
        except asyncio.CancelledError as error:
            primary = error
            raise
        except GeneratorExit as error:
            primary = error
            raise
        except Exception as error:
            logger.exception("Deep Agents to AG-UI stream conversion failed")
            try:
                await close_upstream(None)
            except Exception as close_error:
                # Both the stream and its cleanup share one public error terminal.
                error.add_note(
                    "upstream close also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
                logger.exception(
                    "Closing the upstream after AG-UI conversion failure also failed"
                )
            for event in adapter.abort(code="runtime_error"):
                yield event
            if started and not terminal:
                terminal = True
                yield lifecycle.failed(
                    run_id=run_id,
                    message="Agent run failed",
                    code="runtime_error",
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            await close_upstream(primary)

    batched = micro_batch(converted())
    async with aclosing(batched):
        async for event in batched:
            yield event
