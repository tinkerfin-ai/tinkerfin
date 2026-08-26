"""会话历史分页、详情和事实事件查询"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import NoReturn, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from tinkerfin_messaging.errors import MessagingError, RunNotFound
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.models import ConversationEvent, ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import conversation_identity
from tinkerfin_studio.conversation.schemas import (
    ConversationEventEnvelope,
    ConversationHistoryDetail,
    ConversationHistoryGroupConfig,
    ConversationHistoryListItem,
    ConversationHistoryListResponse,
)

_HISTORY_PAGE_SIZE_MAX = 100
_HISTORY_DAY_RANGES = (7, 30)
_EVENT_LIMIT_MAX = 1000
_SNAPSHOT_VERSION = 3
_EVENT_SCHEMA_VERSION = 3
_PROTOCOL_VERSION = "ag-ui-protocol@0.1.19"


class _HistoryCursorPayload(BaseModel):
    """历史 keyset 游标的严格边界数据"""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    pinned: bool = Field(description="游标行是否置顶")
    updated_at: datetime = Field(
        alias="updatedAt", description="游标行的无时区最近会话活动时间"
    )
    row_id: int = Field(alias="id", gt=0, description="游标行数据库主键")
    query: str | None = Field(
        default=None,
        max_length=255,
        description="游标所属的规范化标题查询词",
    )

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_naive(cls, value: datetime) -> datetime:
        """拒绝不能与数据库无时区字段直接比较的时间"""

        if value.utcoffset() is not None:
            raise ValueError("updatedAt 必须是不含时区的 ISO 时间")
        return value


def _history_item(thread: ConversationThread) -> ConversationHistoryListItem:
    return ConversationHistoryListItem(
        id=thread.id,
        threadId=thread.thread_id,
        title=thread.title,
        status=thread.status,
        lastRunId=thread.last_run_id,
        lastModel=thread.last_model,
        lastSeq=thread.last_seq,
        messageCount=thread.message_count,
        toolCallCount=thread.tool_call_count,
        hasPendingInterrupt=thread.has_pending_interrupt,
        pinned=thread.pinned,
        createdAt=thread.created_at,
        updatedAt=thread.updated_at,
    )


def _history_schema_mismatch(reason: str) -> NoReturn:
    raise SystemException(
        ConversationErrorCode.HISTORY_SCHEMA_MISMATCH
    ) from ValueError(reason)


def _validated_snapshot(thread: ConversationThread) -> dict[str, JsonValue] | None:
    snapshot = thread.snapshot_json
    if thread.snapshot_version != _SNAPSHOT_VERSION:
        _history_schema_mismatch(
            f"thread snapshot_version={thread.snapshot_version}, expected=3"
        )
    if snapshot is None:
        if thread.snapshot_seq != 0:
            _history_schema_mismatch(
                f"empty snapshot with snapshot_seq={thread.snapshot_seq}"
            )
        return None
    if snapshot.get("snapshotVersion") != _SNAPSHOT_VERSION:
        _history_schema_mismatch("snapshot JSON version is not 3")
    if snapshot.get("snapshotSeq") != thread.snapshot_seq:
        _history_schema_mismatch("snapshot JSON sequence does not match snapshot_seq")
    return cast(dict[str, JsonValue], snapshot)


def _event_envelope(event: ConversationEvent) -> ConversationEventEnvelope:
    if event.schema_version != _EVENT_SCHEMA_VERSION:
        _history_schema_mismatch(
            f"event schema_version={event.schema_version}, expected=3"
        )
    if event.protocol_version != _PROTOCOL_VERSION:
        _history_schema_mismatch(
            f"event protocol_version={event.protocol_version!r}, "
            f"expected={_PROTOCOL_VERSION!r}"
        )
    return ConversationEventEnvelope(
        seq=event.seq,
        eventId=event.event_id,
        eventType=event.event_type,
        runId=event.run_id,
        event=cast(JsonValue, event.event_json),
        createdAt=event.created_at,
    )


class ConversationHistoryService:
    """查询前先追赶 Redis 事实日志的会话历史服务"""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        user_id: int,
        projector: ConversationProjectionCoordinator,
    ) -> None:
        self._repository = repository
        self._user_id = user_id
        self._projector = projector

    async def list_history(
        self,
        *,
        page_size: int,
        cursor: str | None,
        query: str | None = None,
    ) -> ConversationHistoryListResponse:
        """按标题查询、置顶和最近会话活动时间稳定分页"""

        resolved_query = (query.strip() or None) if query is not None else None
        resolved = self._decode_cursor(cursor, query=resolved_query)
        resolved_page_size = min(max(page_size, 1), _HISTORY_PAGE_SIZE_MAX)
        threads = await self._repository.list_threads(
            user_id=self._user_id,
            page_size=resolved_page_size,
            cursor=resolved,
            query=resolved_query,
        )
        has_more = len(threads) > resolved_page_size
        page = threads[:resolved_page_size]
        next_cursor = (
            self._encode_cursor(page[-1], query=resolved_query)
            if has_more and page
            else None
        )
        return ConversationHistoryListResponse(
            items=[_history_item(thread) for thread in page],
            nextCursor=next_cursor,
        )

    @staticmethod
    def group_config() -> ConversationHistoryGroupConfig:
        """返回前端按本地自然日分组使用的服务端范围"""

        return ConversationHistoryGroupConfig(dayRanges=list(_HISTORY_DAY_RANGES))

    async def get_detail(self, thread_id: str) -> ConversationHistoryDetail:
        """追赶事件后返回可信快照和尾部"""

        thread = await self._require_thread(thread_id)
        await self._reconcile(thread)
        refreshed = await self._require_thread(thread_id, reload=True)
        snapshot = _validated_snapshot(refreshed)
        events = await self._repository.list_events(
            thread_pk=refreshed.id,
            after_seq=refreshed.snapshot_seq,
            limit=_EVENT_LIMIT_MAX,
        )
        return ConversationHistoryDetail(
            id=refreshed.id,
            threadId=refreshed.thread_id,
            title=refreshed.title,
            status=refreshed.status,
            lastRunId=refreshed.last_run_id,
            lastModel=refreshed.last_model,
            lastSeq=refreshed.last_seq,
            snapshotSeq=refreshed.snapshot_seq,
            snapshotVersion=_SNAPSHOT_VERSION,
            messageCount=refreshed.message_count,
            toolCallCount=refreshed.tool_call_count,
            hasPendingInterrupt=refreshed.has_pending_interrupt,
            pinned=refreshed.pinned,
            snapshot=snapshot,
            events=[_event_envelope(event) for event in events],
            createdAt=refreshed.created_at,
            updatedAt=refreshed.updated_at,
        )

    async def list_events(
        self,
        *,
        thread_id: str,
        after_seq: int | None,
        limit: int,
    ) -> list[ConversationEventEnvelope]:
        """追赶后返回指定序号之后的连续事件"""

        thread = await self._require_thread(thread_id)
        await self._reconcile(thread)
        events = await self._repository.list_events(
            thread_pk=thread.id,
            after_seq=after_seq or 0,
            limit=min(max(limit, 1), _EVENT_LIMIT_MAX),
        )
        return [_event_envelope(event) for event in events]

    async def _reconcile(self, thread: ConversationThread) -> None:
        thread_pk = thread.id
        identity = conversation_identity(
            thread.thread_id,
            thread.last_run_id or "projection-read",
        )
        # 归属查询完成后释放隐式只读事务，再读取 Messaging 事实日志
        await self._repository.commit()
        try:
            await self._projector.reconcile(
                thread_pk=thread_pk,
                identity=identity,
            )
        except RunNotFound:
            return
        except MessagingError as error:
            raise SystemException(
                ConversationErrorCode.EVENT_PROJECTION_UNAVAILABLE
            ) from error

    async def _require_thread(
        self,
        thread_id: str,
        *,
        reload: bool = False,
    ) -> ConversationThread:
        if reload:
            query = self._repository.reload_thread
        else:
            query = self._repository.get_thread
        thread = await query(user_id=self._user_id, thread_id=thread_id)
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        return thread

    @staticmethod
    def _encode_cursor(
        thread: ConversationThread,
        *,
        query: str | None = None,
    ) -> str:
        payload = json.dumps(
            {
                "pinned": thread.pinned,
                "updatedAt": thread.updated_at.isoformat(),
                "id": thread.id,
                "query": query,
            },
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @staticmethod
    def _decode_cursor(
        value: str | None,
        *,
        query: str | None = None,
    ) -> tuple[bool, datetime, int] | None:
        if value is None:
            return None
        try:
            decoded = base64.b64decode(
                value.encode(),
                altchars=b"-_",
                validate=True,
            )
            payload = _HistoryCursorPayload.model_validate_json(decoded, strict=True)
            if payload.query != query:
                raise ValueError("游标查询词与当前请求不一致")
            return payload.pinned, payload.updated_at, payload.row_id
        except (ValueError, TypeError, ValidationError):
            raise BusinessException(ConversationErrorCode.INVALID_CURSOR) from None


__all__ = ["ConversationHistoryService"]
