"""统一 API 响应包络"""

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Generic, TypeVar

from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一业务响应结构"""

    code: int = Field(default=0, description="业务状态码，0 表示成功")
    message: str = Field(default="success", description="可安全展示的业务消息")
    data: T | None = Field(default=None, description="业务数据")

    @classmethod
    def success(cls, data: T | None = None) -> "ApiResponse[T]":
        """构造成功响应"""

        return cls(data=data)


def sse_response(body: AsyncIterator[str | bytes]) -> StreamingResponse:
    """用 Studio 统一响应策略承载已编码的 SSE 内容"""

    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def trace_sse_response(
    events: AsyncGenerator[BaseModel, None],
) -> StreamingResponse:
    """以原生事件流编码 Trace，并在结束或断连时关闭上游

    长等待必须来自 Trace 查询返回的可关闭 follower。该 follower 已拥有取消和数据库清理；
    此处只承担 Studio 的事件名称、JSON 编码与 HTTP 响应策略。

    Args:
        events: 基于 Trace follower 生成的单次消费业务事件流

    Returns:
        可由 Starlette 直接发送的原生 SSE 响应
    """

    return sse_response(_trace_sse(events))


async def _trace_sse(
    events: AsyncGenerator[BaseModel, None],
) -> AsyncGenerator[bytes, None]:
    """逐条输出 Trace SSE 帧，并保证单次事件流归还资源"""

    try:
        async for event in events:
            payload = event.model_dump_json(
                by_alias=True,
                exclude_none=False,
            ).encode()
            yield b"event: trace\ndata: " + payload + b"\n\n"
    finally:
        await events.aclose()
