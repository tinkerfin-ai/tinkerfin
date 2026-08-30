"""AG-UI 实时流、Trace 历史和会话命令 HTTP 入口"""

from collections.abc import AsyncGenerator
from typing import Annotated, TypeAlias

from ag_ui.core import RunAgentInput
from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.responses import StreamingResponse

from tinkerfin_studio.api.dependencies import (
    ConversationCommandDep,
    ConversationHistoryDep,
    SessionDep,
    UserContextDep,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.schemas import (
    CancelRunResponse,
    ConversationHistoryDetail,
    ConversationHistoryGroupConfig,
    ConversationHistoryListItem,
    ConversationHistoryListResponse,
    ConversationThreadUpdate,
)
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/conversation", tags=["会话"])

ThreadIdPath: TypeAlias = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        description="会话 threadId",
    ),
]


@router.get("/history", response_model=ApiResponse[ConversationHistoryListResponse])
async def list_history(
    service: ConversationHistoryDep,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    query: Annotated[str | None, Query(max_length=255)] = None,
) -> ApiResponse[ConversationHistoryListResponse]:
    """分页返回当前用户历史会话"""

    return ApiResponse.success(
        await service.list_history(page_size=page_size, cursor=cursor, query=query)
    )


@router.get("/config", response_model=ApiResponse[ConversationHistoryGroupConfig])
async def get_conversation_config(
    service: ConversationHistoryDep,
) -> ApiResponse[ConversationHistoryGroupConfig]:
    """返回历史会话分组等前端查询配置"""

    return ApiResponse.success(service.group_config())


@router.get(
    "/{thread_id}/history", response_model=ApiResponse[ConversationHistoryDetail]
)
async def get_history(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
    history_cursor: Annotated[
        str | None,
        Query(alias="historyCursor", min_length=1),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> ApiResponse[ConversationHistoryDetail]:
    """返回一个会话的固定前缀 Trace 视图"""

    return ApiResponse.success(
        await service.get_detail(
            thread_id,
            history_cursor=history_cursor,
            limit=limit,
        )
    )


@router.get("/{thread_id}/trace", response_class=StreamingResponse)
async def follow_trace(
    thread_id: ThreadIdPath,
    service: ConversationHistoryDep,
) -> StreamingResponse:
    """鉴权后先发送 Trace snapshot，再持续发送语义增量"""

    events = await service.follow_trace(thread_id)
    return StreamingResponse(
        _trace_sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    input_data: RunAgentInput,
    request: Request,
    session: SessionDep,
    user: UserContextDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """启动或附着 durable Agent run 并返回 AG-UI SSE"""

    try:
        chat_request = ChatRequest.from_agui(input_data)
    except ValidationError as error:
        raise RequestValidationError(error.errors(include_input=False)) from error
    prepared = await ConversationChatService(
        session,
        user=user,
        resources=get_resources(request.app),
    ).start(chat_request, last_event_id=last_event_id)
    return StreamingResponse(
        _chat_sse(prepared.body),
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


async def _trace_sse(
    events: AsyncGenerator[BaseModel, None],
) -> AsyncGenerator[bytes, None]:
    """逐条编码 Trace 事件，并在断连时关闭框架 follow iterator"""

    try:
        async for event in events:
            payload = event.model_dump_json(by_alias=True, exclude_none=False)
            yield f"event: trace\ndata: {payload}\n\n".encode()
    finally:
        await events.aclose()


async def _chat_sse(
    body: AsyncGenerator[bytes, None],
) -> AsyncGenerator[bytes, None]:
    """转发框架 SSE，并在 HTTP 断连或响应终止时释放订阅"""

    try:
        async for frame in body:
            yield frame
    finally:
        await body.aclose()
