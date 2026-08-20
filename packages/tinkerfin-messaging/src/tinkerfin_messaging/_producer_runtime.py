"""Owned producer, settlement, recovery, and lease supervision."""

from __future__ import annotations

__all__ = [
    "_codec_id",
    "_open_recoverable_source",
    "_producer_finished",
    "_start_producer",
    "_start_producer_task",
    "_start_recoverable_producer",
]

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Literal, TypeAlias, TypeVar, cast

from ._messaging_boundary import (
    _await_backend,
    _ContextCancelCallback,
    _derived_message_id,
    _invoke_cancel,
    _read_backend,
)
from .backend import PreparedRun
from .errors import BackendOwnershipLost
from .models import RecoverableMessage, RecoveryCheckpoint
from .protocols import MessageCodec, MessageSource, RecoverableSource

if TYPE_CHECKING:
    from .messaging import CommittedCallback, Messaging

SourceT = TypeVar("SourceT")
ReplayT = TypeVar("ReplayT")
ProducedT = TypeVar("ProducedT")

_ProducerFailureStage: TypeAlias = Literal[
    "callback",
    "commit",
    "finish",
    "lease",
    "settlement",
    "shutdown",
    "source",
    "source_close",
    "tail",
]

logger = logging.getLogger("tinkerfin_messaging.messaging")


@dataclass(slots=True)
class _ProducerState:
    cancel_requested: bool = False
    settling: bool = False
    ownership_lost: bool = False
    ownership_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _ProducedMessage(Generic[SourceT]):
    data: SourceT
    message_id: str
    checkpoint: RecoveryCheckpoint | None


@dataclass(slots=True)
class _PendingCommit(Generic[ProducedT]):
    item: ProducedT
    acknowledged: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None


def _start_producer(
    self: Messaging,
    *,
    prepared: PreparedRun,
    source: MessageSource[SourceT],
    codec: MessageCodec[SourceT, ReplayT],
    cancel: _ContextCancelCallback[SourceT] | None,
    on_committed: CommittedCallback | None,
) -> asyncio.Event:
    def prepare_item(item: SourceT, ordinal: int) -> _ProducedMessage[SourceT]:
        return _ProducedMessage(
            data=item,
            message_id=_derived_message_id(prepared.handle.identity, ordinal),
            checkpoint=None,
        )

    return self._start_producer_task(
        prepared=prepared,
        source=source,
        codec=codec,
        cancel=cancel,
        on_committed=on_committed,
        prepare_item=prepare_item,
    )


async def _open_recoverable_source(
    self: Messaging,
    *,
    prepared: PreparedRun,
    source: RecoverableSource[SourceT],
) -> MessageSource[RecoverableMessage[SourceT]]:
    """Keep distributed ownership alive while a source rebuilds its state."""

    interval = _read_backend(
        "lease_renew_interval",
        lambda: self.backend.lease_renew_interval,
    )
    if interval is None:
        return await source.open(prepared.checkpoint)

    opening = asyncio.create_task(
        source.open(prepared.checkpoint),
        name=(f"tinkerfin-messaging-source-open:{prepared.handle.identity.run_id}"),
    )

    async def renew_until_opened() -> None:
        while True:
            await asyncio.sleep(interval)
            renewed = await _await_backend(
                "renew",
                self.backend.renew(prepared.handle),
            )
            if not renewed:
                raise BackendOwnershipLost(
                    "Producer for run "
                    f"{prepared.handle.identity.run_id!r} lost its lease "
                    "while reopening its source"
                )

    renewing = asyncio.create_task(
        renew_until_opened(),
        name=(f"tinkerfin-messaging-open-lease:{prepared.handle.identity.run_id}"),
    )
    claimed = False
    try:
        done, _ = await asyncio.wait(
            {opening, renewing},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if renewing in done:
            renewal_error = renewing.exception()
            if renewal_error is not None:
                if opening.done() and not opening.cancelled():
                    try:
                        opening.result()
                    except BaseException as opening_error:
                        raise renewal_error from opening_error
                raise renewal_error
        opened = opening.result()
        claimed = True
        return opened
    finally:
        if not opening.done():
            opening.cancel()
        if not renewing.done():
            renewing.cancel()
        opening_result, _ = await asyncio.gather(
            opening,
            renewing,
            return_exceptions=True,
        )
        if not claimed and not isinstance(opening_result, BaseException):
            await opening_result.aclose()


def _start_recoverable_producer(
    self: Messaging,
    *,
    prepared: PreparedRun,
    source: MessageSource[RecoverableMessage[SourceT]],
    codec: MessageCodec[SourceT, ReplayT],
    cancel: _ContextCancelCallback[RecoverableMessage[SourceT]] | None,
    on_committed: CommittedCallback | None,
) -> asyncio.Event:
    def prepare_item(
        item: RecoverableMessage[SourceT],
        ordinal: int,
    ) -> _ProducedMessage[SourceT]:
        del ordinal
        if item.checkpoint.last_message_id != item.message_id:
            raise ValueError(
                "RecoverableMessage checkpoint.last_message_id must match message_id"
            )
        return _ProducedMessage(
            data=item.data,
            message_id=item.message_id,
            checkpoint=item.checkpoint,
        )

    return self._start_producer_task(
        prepared=prepared,
        source=source,
        codec=codec,
        cancel=cancel,
        on_committed=on_committed,
        prepare_item=prepare_item,
    )


def _start_producer_task(
    self: Messaging,
    *,
    prepared: PreparedRun,
    source: MessageSource[ProducedT],
    codec: MessageCodec[SourceT, ReplayT],
    cancel: _ContextCancelCallback[ProducedT] | None,
    on_committed: CommittedCallback | None,
    prepare_item: Callable[[ProducedT, int], _ProducedMessage[SourceT]],
) -> asyncio.Event:
    from .messaging import CancelContext

    state = _ProducerState()
    started = asyncio.Event()
    cancel_context = CancelContext(
        channel=prepared.handle.channel,
        identity=prepared.handle.identity,
    )

    async def produce() -> None:
        # `wrap()` waits for this first turn so later task cancellation always
        # enters the producer's cleanup path and cannot strand source ownership.
        started.set()
        producer_task = asyncio.current_task()
        if producer_task is None:
            raise RuntimeError("Messaging producer requires an asyncio task")
        status: Literal["completed", "cancelled", "failed"] = "completed"
        error: BaseException | None = None
        error_stage: _ProducerFailureStage | None = None
        cancel_watcher: asyncio.Task[Iterable[ProducedT] | None] | None = None
        cancel_callback_task: asyncio.Task[Iterable[ProducedT] | None] | None = None
        settlement_task: asyncio.Task[bool] | None = None
        lease_renewer: asyncio.Task[None] | None = None
        pending_commits: asyncio.Queue[_PendingCommit[ProducedT]] = asyncio.Queue(
            maxsize=1
        )
        commit_capacity = asyncio.Semaphore(1)
        commit_error: BaseException | None = None
        next_ordinal = 1
        shutdown_requested = False

        def retain_secondary_failure(
            *,
            stage: _ProducerFailureStage,
            secondary: BaseException,
        ) -> None:
            primary = error
            primary_stage = error_stage
            if primary is None or primary_stage is None:
                raise RuntimeError(
                    "Messaging cannot retain a secondary failure without a primary"
                )
            primary.add_note(
                f"Messaging {stage} also failed after {primary_stage}: "
                f"{type(secondary).__name__}: {secondary}"
            )
            logger.error(
                "Messaging producer retained a secondary failure",
                extra={
                    "channel": prepared.handle.channel,
                    "thread_id": prepared.handle.identity.thread_id,
                    "run_id": prepared.handle.identity.run_id,
                    "primary_stage": primary_stage,
                    "secondary_stage": stage,
                    "ownership_lost": isinstance(
                        secondary,
                        BackendOwnershipLost,
                    ),
                },
                exc_info=(
                    type(secondary),
                    secondary,
                    secondary.__traceback__,
                ),
            )

        def record_failure(
            *,
            stage: _ProducerFailureStage,
            failure: BaseException,
        ) -> None:
            nonlocal error, error_stage, status
            if error is None:
                error = failure
                error_stage = stage
                status = "failed"
                return
            if (
                error_stage == "shutdown"
                and isinstance(error, asyncio.CancelledError)
                and not isinstance(failure, asyncio.CancelledError)
            ):
                failure.add_note(
                    "Messaging shutdown also cancelled the producer before "
                    f"{stage} failed: {error}"
                )
                error = failure
                error_stage = stage
                status = "failed"
                return
            if failure is error:
                return
            retain_secondary_failure(stage=stage, secondary=failure)

        async def commit_messages() -> None:
            nonlocal commit_error, next_ordinal
            while True:
                pending = await pending_commits.get()
                try:
                    if commit_error is not None:
                        pending.error = commit_error
                        continue
                    produced = prepare_item(pending.item, next_ordinal)
                    payload = codec.encode(produced.data)
                    if not isinstance(payload, bytes):
                        raise TypeError("MessageCodec.encode() must return bytes")
                    envelope = await _await_backend(
                        "append",
                        self.backend.append(
                            prepared.handle,
                            message_id=produced.message_id,
                            codec=self._codec_id(codec),
                            payload=payload,
                            checkpoint=produced.checkpoint,
                        ),
                    )
                    next_ordinal += 1
                    if on_committed is not None:
                        try:
                            await on_committed(envelope)
                        except Exception as observer_error:  # noqa: BLE001 - observer isolation
                            logger.error(
                                "Committed message hook failed: "
                                "channel=%s thread_id=%s run_id=%s seq=%s "
                                "error_type=%s",
                                envelope.channel,
                                envelope.identity.thread_id,
                                envelope.identity.run_id,
                                envelope.seq,
                                type(observer_error).__name__,
                            )
                except asyncio.CancelledError as append_cancellation:
                    pending.error = append_cancellation
                    raise
                except BaseException as append_error:  # noqa: BLE001 - producer outcome
                    if commit_error is None:
                        commit_error = append_error
                    pending.error = commit_error
                finally:
                    pending.acknowledged.set()
                    commit_capacity.release()
                    pending_commits.task_done()

        committer = asyncio.create_task(
            commit_messages(),
            name=(f"tinkerfin-messaging-committer:{prepared.handle.identity.run_id}"),
        )

        def enqueue_acquired(item: ProducedT) -> _PendingCommit[ProducedT]:
            pending = _PendingCommit(item=item)
            try:
                pending_commits.put_nowait(pending)
            except BaseException:
                commit_capacity.release()
                raise
            return pending

        async def wait_committed(pending: _PendingCommit[ProducedT]) -> None:
            await pending.acknowledged.wait()
            if pending.error is not None:
                raise pending.error.with_traceback(pending.error.__traceback__)

        async def submit_tail(item: ProducedT) -> None:
            await commit_capacity.acquire()
            pending = enqueue_acquired(item)
            await wait_committed(pending)

        async def consume_source() -> None:
            consumer_task = asyncio.current_task()
            if consumer_task is None:
                raise RuntimeError("Producer source requires an asyncio task")
            iterator = aiter(source)
            while True:
                await commit_capacity.acquire()
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    commit_capacity.release()
                    return
                except BaseException:
                    commit_capacity.release()
                    raise
                pending = enqueue_acquired(item)
                await wait_committed(pending)
                if consumer_task.cancelling():
                    raise asyncio.CancelledError

        source_consumer = asyncio.create_task(
            consume_source(),
            name=(f"tinkerfin-messaging-source:{prepared.handle.identity.run_id}"),
        )

        def claim_settlement() -> asyncio.Task[bool]:
            nonlocal settlement_task
            existing = settlement_task
            if existing is not None:
                return existing
            settlement_task = asyncio.create_task(
                _await_backend(
                    "begin_settlement",
                    self.backend.begin_settlement(prepared.handle),
                ),
                name=(
                    f"tinkerfin-messaging-settlement:{prepared.handle.identity.run_id}"
                ),
            )
            return settlement_task

        def claim_cancel_callback() -> tuple[
            asyncio.Task[Iterable[ProducedT] | None],
            bool,
        ]:
            nonlocal cancel_callback_task
            existing = cancel_callback_task
            if existing is not None:
                return existing, False
            callback = cancel
            if callback is None:
                raise RuntimeError(
                    "backend accepted cancellation without a registered callback"
                )
            state.cancel_requested = True
            self._settling_producers.add(cast(asyncio.Task[None], producer_task))

            async def invoke() -> Iterable[ProducedT] | None:
                try:
                    return await _invoke_cancel(callback, cancel_context)
                except BaseException:
                    if not source_consumer.done():
                        source_consumer.cancel()
                    raise

            cancel_callback_task = asyncio.create_task(
                invoke(),
                name=(
                    "tinkerfin-messaging-cancel-callback:"
                    f"{prepared.handle.identity.run_id}"
                ),
            )
            return cancel_callback_task, True

        async def watch_cancel() -> Iterable[ProducedT] | None:
            requested = await _await_backend(
                "wait_for_cancel",
                self.backend.wait_for_cancel(prepared.handle),
            )
            if not requested:
                return None
            cancel_accepted = await asyncio.shield(claim_settlement())
            if not cancel_accepted:
                return None
            callback_task, _ = claim_cancel_callback()
            return await callback_task

        if cancel is not None:
            cancel_watcher = asyncio.create_task(
                watch_cancel(),
                name=(f"tinkerfin-messaging-cancel:{prepared.handle.identity.run_id}"),
            )

        async def renew_lease(interval: float) -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await _await_backend(
                        "renew",
                        self.backend.renew(prepared.handle),
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as renew_error:  # noqa: BLE001 - fence safety
                    state.ownership_lost = True
                    state.ownership_error = renew_error
                    source_consumer.cancel()
                    return
                if not renewed:
                    state.ownership_lost = True
                    state.ownership_error = BackendOwnershipLost(
                        "Producer for run "
                        f"{prepared.handle.identity.run_id!r} lost its lease"
                    )
                    source_consumer.cancel()
                    return

        renew_interval = _read_backend(
            "lease_renew_interval",
            lambda: self.backend.lease_renew_interval,
        )
        if renew_interval is not None:
            lease_renewer = asyncio.create_task(
                renew_lease(renew_interval),
                name=(f"tinkerfin-messaging-lease:{prepared.handle.identity.run_id}"),
            )
        try:
            await asyncio.shield(source_consumer)
            if state.cancel_requested:
                status = "cancelled"
        except asyncio.CancelledError as cancellation:
            producer_task = asyncio.current_task()
            shutdown_requested = (
                producer_task is not None and producer_task.cancelling() > 0
            )
            if state.ownership_lost:
                record_failure(
                    stage="lease",
                    failure=state.ownership_error or cancellation,
                )
            else:
                status = "cancelled" if state.cancel_requested else "failed"
                if state.cancel_requested:
                    error = None
                    error_stage = None
                else:
                    error = cancellation
                    error_stage = "shutdown" if shutdown_requested else "source"
        except BackendOwnershipLost as ownership_error:
            state.ownership_lost = True
            record_failure(stage="lease", failure=ownership_error)
        except BaseException as producer_error:  # noqa: BLE001 - record producer outcome
            record_failure(
                stage=("commit" if producer_error is commit_error else "source"),
                failure=producer_error,
            )
        finally:
            producer_task = asyncio.current_task()
            if producer_task is not None:
                self._settling_producers.add(cast(asyncio.Task[None], producer_task))
            cancel_won = False
            try:
                cancel_won = await asyncio.shield(claim_settlement())
            except BackendOwnershipLost as ownership_error:
                state.ownership_lost = True
                record_failure(stage="settlement", failure=ownership_error)
            except BaseException as settlement_error:  # noqa: BLE001
                record_failure(stage="settlement", failure=settlement_error)
            state.settling = True
            cancel_watcher_settled = False
            cancel_error: BaseException | None = None
            cancel_tail: Iterable[ProducedT] | None = None

            if cancel_won:
                if not state.ownership_lost and (
                    status == "completed"
                    or shutdown_requested
                    or isinstance(error, asyncio.CancelledError)
                ):
                    status = "cancelled"
                    error = None
                    error_stage = None
                try:
                    callback_task, claimed_here = claim_cancel_callback()
                except BaseException as callback_error:  # noqa: BLE001
                    cancel_error = callback_error
                else:
                    if (
                        claimed_here
                        and cancel_watcher is not None
                        and not cancel_watcher.done()
                    ):
                        cancel_watcher.cancel()
                        await asyncio.gather(
                            cancel_watcher,
                            return_exceptions=True,
                        )
                        cancel_watcher_settled = True
                    try:
                        cancel_tail = await callback_task
                    except BaseException as callback_error:  # noqa: BLE001
                        cancel_error = callback_error
                    if cancel_watcher is not None and not cancel_watcher_settled:
                        await asyncio.gather(
                            cancel_watcher,
                            return_exceptions=True,
                        )
                        cancel_watcher_settled = True
            elif cancel_watcher is not None:
                if not cancel_watcher.done():
                    cancel_watcher.cancel()
                await asyncio.gather(
                    cancel_watcher,
                    return_exceptions=True,
                )
                cancel_watcher_settled = True

            if not source_consumer.done():
                source_consumer.cancel()
            await asyncio.gather(source_consumer, return_exceptions=True)

            if shutdown_requested and not cancel_won:
                if cancel_watcher is not None:
                    cancel_watcher_settled = True
                if not committer.done():
                    committer.cancel()
                await asyncio.gather(committer, return_exceptions=True)
                while True:
                    try:
                        pending = pending_commits.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    pending.error = asyncio.CancelledError()
                    pending.acknowledged.set()
                    commit_capacity.release()
                    pending_commits.task_done()
            if cancel_error is not None:
                record_failure(stage="callback", failure=cancel_error)
            elif cancel_tail is not None:
                try:
                    for item in cancel_tail:
                        await submit_tail(item)
                except BaseException as tail_error:  # noqa: BLE001
                    record_failure(
                        stage=("commit" if tail_error is commit_error else "tail"),
                        failure=tail_error,
                    )
            await pending_commits.join()
            if commit_error is not None:
                record_failure(stage="commit", failure=commit_error)
            if not committer.done():
                committer.cancel()
            committer_result = await asyncio.gather(
                committer,
                return_exceptions=True,
            )
            unexpected_committer_error = next(
                (
                    result
                    for result in committer_result
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ),
                None,
            )
            if unexpected_committer_error is not None:
                record_failure(
                    stage="commit",
                    failure=unexpected_committer_error,
                )
            try:
                await source.aclose()
            except BaseException as close_error:  # noqa: BLE001 - cleanup is part of outcome
                record_failure(stage="source_close", failure=close_error)
            try:
                await _await_backend(
                    "finish",
                    self.backend.finish(
                        prepared.handle,
                        status=status,
                        error=error,
                    ),
                )
            except BaseException as finish_error:
                if error is None:
                    raise
                record_failure(stage="finish", failure=finish_error)
                assert error is not None
                raise error.with_traceback(error.__traceback__)
            finally:
                if cancel_watcher is not None and not cancel_watcher_settled:
                    if not cancel_watcher.done():
                        cancel_watcher.cancel()
                    await asyncio.gather(
                        cancel_watcher,
                        return_exceptions=True,
                    )
                if lease_renewer is not None:
                    if not lease_renewer.done():
                        lease_renewer.cancel()
                    await asyncio.gather(lease_renewer, return_exceptions=True)

    task = asyncio.create_task(
        produce(),
        name=(f"tinkerfin-messaging-producer:{prepared.handle.identity.run_id}"),
    )
    self._producer_tasks.add(task)
    task.add_done_callback(self._producer_finished)
    return started


def _producer_finished(self: Messaging, task: asyncio.Task[None]) -> None:
    """Remove and consume a settled owned task to prevent orphan warnings."""

    self._producer_tasks.discard(task)
    self._settling_producers.discard(task)
    if not task.cancelled():
        task.exception()


def _codec_id(codec: MessageCodec[SourceT, ReplayT]) -> str:
    return codec.codec_id
