"""会话流、历史和线程命令 HTTP 入口"""

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request
from starlette.responses import StreamingResponse

from tinkerfin_studio.api.dependencies import (
    ConversationCommandDep,
    ConversationHistoryDep,
    SessionDep,
    UserContextDep,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.conversation.request import THREAD_ID_PATTERN, ChatRequest
from tinkerfin_studio.conversation.schemas import (
    CancelRunResponse,
    ConversationEventEnvelope,
    ConversationHistoryDetail,
    ConversationHistoryListItem,
    ConversationHistoryListResponse,
    ConversationThreadUpdate,
)
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/conversation", tags=["会话"])

ThreadIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=THREAD_ID_PATTERN,
        description="会话 threadId",
    ),
]


@router.get("/history", response_model=ApiResponse[ConversationHistoryListResponse])
async def list_history(
    service: ConversationHistoryDep,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
) -> ApiResponse[ConversationHistoryListResponse]:
    """分页返回当前用户历史会话"""

    return ApiResponse.success(
        await service.list_history(page_size=page_size, cursor=cursor)
    )


@router.get(
    "/{thread_id}/history", response_model=ApiResponse[ConversationHistoryDetail]
)
async def get_history(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
) -> ApiResponse[ConversationHistoryDetail]:
    """返回一个会话的可信快照和尾部事件"""

    return ApiResponse.success(await service.get_detail(thread_id))


@router.get(
    "/{thread_id}/events", response_model=ApiResponse[list[ConversationEventEnvelope]]
)
async def list_events(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
    after_seq: Annotated[int | None, Query(alias="afterSeq", ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ApiResponse[list[ConversationEventEnvelope]]:
    """返回 afterSeq 之后的连续已提交事件"""

    return ApiResponse.success(
        await service.list_events(
            thread_id=thread_id,
            after_seq=after_seq,
            limit=limit,
        )
    )


@router.patch("/{thread_id}", response_model=ApiResponse[ConversationHistoryListItem])
async def update_thread(
    thread_id: ThreadIdPath,
    payload: ConversationThreadUpdate,
    service: ConversationCommandDep,
) -> ApiResponse[ConversationHistoryListItem]:
    """重命名或置顶会话"""

    return ApiResponse.success(
        await service.update(
            thread_id=thread_id,
            title=payload.title,
            pinned=payload.pinned,
        )
    )


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: ThreadIdPath,
    service: ConversationCommandDep,
) -> None:
    """删除已终止会话的全部关联数据"""

    await service.delete(thread_id=thread_id)


@router.post("/chat", response_class=StreamingResponse)
async def chat(
    input_data: ChatRequest,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """创建请求级 Deep Agent graph 并返回 durable AG-UI SSE"""

    prepared = await ConversationChatService(
        session,
        user=user,
        resources=get_resources(request.app),
    ).start(input_data, last_event_id=last_event_id)
    return StreamingResponse(
        prepared.body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{thread_id}/runs/{run_id}/cancel",
    response_model=ApiResponse[CancelRunResponse],
)
async def cancel_run(
    thread_id: ThreadIdPath,
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> ApiResponse[CancelRunResponse]:
    """请求取消当前用户的指定 durable run"""

    service = ConversationChatService(
        session,
        user=user,
        resources=get_resources(request.app),
    )
    return ApiResponse.success(await service.cancel(thread_id=thread_id, run_id=run_id))
