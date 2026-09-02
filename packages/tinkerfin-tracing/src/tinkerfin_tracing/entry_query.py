"""Opaque entry cursors and closeable filtered query following."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from pydantic import Field, ValidationError, field_validator

from ._models import TraceModel
from .entries import (
    TraceEntry,
    TraceEntryCompleteness,
    TraceEntryDelta,
    TraceEntryPage,
    TraceFacets,
    TraceFilter,
    TraceTurn,
)
from .errors import InvalidTraceCursor
from .follow import TraceFollow, _close_trace_source, _trace_follow
from .store import TraceStore, TraceThreadKey


class _EntryCursor(TraceModel):
    namespace: str = Field(min_length=1, max_length=2048)
    thread_id: str = Field(min_length=1, max_length=2048)
    generation: str = Field(min_length=1, max_length=2048)
    head_run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    filter_digest: str = Field(min_length=64, max_length=64)
    before_started_at: datetime
    before_entry_id: str = Field(min_length=1, max_length=2048)

    @field_validator("before_started_at")
    @classmethod
    def time_is_utc(cls, value: datetime) -> datetime:
        """Reject forged cursors whose ordering time is not aware UTC."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Trace entry cursor time must be aware UTC")
        return value


def _filter_digest(where: TraceFilter) -> str:
    """Return a stable fingerprint for one validated functional filter."""

    canonical = {
        "kinds": sorted(value.value for value in where.kinds),
        "statuses": sorted(value.value for value in where.statuses),
        "parentId": where.parent_id,
        "agentNames": sorted(where.agent_names),
        "middlewareNames": sorted(where.middleware_names),
        "skillNames": sorted(where.skill_names),
        "providers": sorted(where.providers),
        "models": sorted(where.models),
        "namespaces": sorted(list(value) for value in where.namespaces),
        "search": where.search,
        "startedAfter": (
            None if where.started_after is None else where.started_after.isoformat()
        ),
        "startedBefore": (
            None if where.started_before is None else where.started_before.isoformat()
        ),
        "includeAncestors": where.include_ancestors,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def encode_entry_cursor(
    *,
    key: TraceThreadKey,
    head_run_id: str | None,
    where: TraceFilter,
    before_started_at: datetime,
    before_entry_id: str,
) -> str:
    """Encode one generation- and filter-bound entry cursor."""

    payload = _EntryCursor(
        namespace=key.namespace,
        thread_id=key.thread_id,
        generation=key.generation,
        head_run_id=head_run_id,
        filter_digest=_filter_digest(where),
        before_started_at=before_started_at,
        before_entry_id=before_entry_id,
    ).model_dump_json(by_alias=True)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_entry_cursor(
    value: str,
    *,
    key: TraceThreadKey,
    head_run_id: str | None,
    where: TraceFilter,
) -> tuple[datetime, str]:
    """Validate and decode one cursor for the exact current query."""

    if not isinstance(value, str) or not value:
        raise InvalidTraceCursor("Trace entry cursor must be non-empty text")
    if len(value) > 16_384:
        raise InvalidTraceCursor("Trace entry cursor exceeds the maximum length")
    try:
        padding = "=" * (-len(value) % 4)
        cursor = _EntryCursor.model_validate_json(
            base64.urlsafe_b64decode((value + padding).encode())
        )
    except (ValueError, ValidationError) as error:
        raise InvalidTraceCursor(
            "Trace entry cursor is invalid", cause=error
        ) from error
    if (
        cursor.namespace != key.namespace
        or cursor.thread_id != key.thread_id
        or cursor.generation != key.generation
        or cursor.head_run_id != head_run_id
        or cursor.filter_digest != _filter_digest(where)
    ):
        raise InvalidTraceCursor("Trace entry cursor belongs to another query")
    return cursor.before_started_at, cursor.before_entry_id


class TraceQuery:
    """Expose one current filtered page and a closeable live replacement stream."""

    __slots__ = ("_page", "_refresh", "_store", "_key")

    def __init__(
        self,
        page: TraceEntryPage,
        *,
        store: TraceStore,
        key: TraceThreadKey,
        refresh: Callable[[], Awaitable[TraceEntryPage]],
    ) -> None:
        """Create a defensive query view over one exact Store generation.

        Args:
            page: Current filtered page.
            store: Borrowed Store used for live following.
            key: Exact generation followed by this query.
            refresh: Re-evaluate the same filter and cursor boundary.
        """

        self._page = page.model_copy(deep=True)
        self._store = store
        self._key = key
        self._refresh = refresh

    @property
    def items(self) -> tuple[TraceEntry, ...]:
        """Return matching entries plus any requested ancestors."""

        return tuple(item.model_copy(deep=True) for item in self._page.items)

    @property
    def turns(self) -> tuple[TraceTurn, ...]:
        """Return the selected-lineage Turns represented by this entry page."""

        return tuple(turn.model_copy(deep=True) for turn in self._page.turns)

    @property
    def next_cursor(self) -> str | None:
        """Return the opaque cursor for the next older matching page."""

        return self._page.next_cursor

    @property
    def as_of_seq(self) -> int:
        """Return the Ledger tail observed with this current index read."""

        return self._page.as_of_seq

    @property
    def facets(self) -> TraceFacets:
        """Return server-computed filter counts."""

        return self._page.facets.model_copy(deep=True)

    @property
    def completeness(self) -> TraceEntryCompleteness:
        """Return explicit historical call-observation gaps."""

        return self._page.completeness.model_copy(deep=True)

    def follow(self) -> TraceFollow[TraceEntryDelta]:
        """Return a closeable follower for changes to this filtered page.

        Returns:
            A single-use follower that emits Turn and entry changes for the same
            generation, filter, and cursor. Use it as an asynchronous context manager
            when iteration may stop early, or call ``aclose()`` explicitly.

        Raises:
            TraceFollowLifecycleError: The returned follower is reused concurrently or
                entered after closing.
            TraceThreadNotFound: The exact generation is deleted or replaced.
            TraceStoreError: Follow or page refresh fails.
        """

        async def updates() -> AsyncIterator[TraceEntryDelta]:
            previous = {item.id: item for item in self._page.items}
            previous_turns = {turn.id: turn for turn in self._page.turns}
            previous_facets = self._page.facets
            previous_completeness = self._page.completeness
            batches = self._store.follow(self._key, after_seq=self._page.as_of_seq)
            primary_error: BaseException | None = None
            try:
                async for _batch in batches:
                    current = await self._refresh()
                    current_items = {item.id: item for item in current.items}
                    current_turns = {turn.id: turn for turn in current.turns}
                    turn_upserts = tuple(
                        turn
                        for turn_id, turn in current_turns.items()
                        if turn_id not in previous_turns
                        or previous_turns[turn_id] != turn
                    )
                    turn_removes = tuple(
                        turn_id
                        for turn_id in previous_turns
                        if turn_id not in current_turns
                    )
                    upserts = tuple(
                        item
                        for entry_id, item in current_items.items()
                        if entry_id not in previous or previous[entry_id] != item
                    )
                    removes = tuple(
                        entry_id
                        for entry_id in previous
                        if entry_id not in current_items
                    )
                    facets_changed = current.facets != previous_facets
                    completeness_changed = current.completeness != previous_completeness
                    previous = current_items
                    previous_turns = current_turns
                    previous_facets = current.facets
                    previous_completeness = current.completeness
                    if not (
                        turn_upserts
                        or turn_removes
                        or upserts
                        or removes
                        or facets_changed
                        or completeness_changed
                    ):
                        continue
                    yield TraceEntryDelta(
                        as_of_seq=current.as_of_seq,
                        turn_upserts=turn_upserts,
                        turn_removes=turn_removes,
                        upserts=upserts,
                        removes=removes,
                        facets=current.facets,
                        completeness=current.completeness,
                    )
            except BaseException as error:
                primary_error = error
                raise
            finally:
                await _close_trace_source(
                    batches,
                    primary_error=primary_error,
                )

        return _trace_follow(updates)


__all__ = ["TraceQuery"]
