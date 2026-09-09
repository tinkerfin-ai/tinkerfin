"""使用公共 Trace 分页 API 查询期重建 Todo Group"""

from __future__ import annotations

import asyncio
from typing import Final

from anyio import CapacityLimiter

from tinkerfin_tracing import TraceThread

from .projector import TodoGroupProjector

_EVENT_PAGE_SIZE: Final = 1_000


class TaskTraceQueryTimeout(TimeoutError):
    """表示任务轨迹未在查询时限内完成重放"""


class TodoGroupQueryExecutor:
    """在短生命周期并发边界内逐页重放固定 Trace 前缀

    限流只覆盖初始查询。返回的 projector 不持有数据库连接，也不占用长期
    容量；需要 follow 的调用方负责在流结束时关闭 projector。
    """

    def __init__(
        self,
        *,
        capacity: int = 2,
        timeout_seconds: float = 30.0,
    ) -> None:
        if capacity < 1:
            raise ValueError("任务轨迹查询容量必须至少为 1")
        if timeout_seconds <= 0:
            raise ValueError("任务轨迹查询时限必须大于 0")
        self._limiter = CapacityLimiter(capacity)
        self._timeout_seconds = timeout_seconds

    @property
    def borrowed_tokens(self) -> int:
        """返回当前正在执行初始重放的查询数量"""

        return self._limiter.borrowed_tokens

    async def project(self, trace: TraceThread) -> TodoGroupProjector:
        """重放一个固定 Trace 前缀并返回可继续消费 follow 更新的 projector

        Args:
            trace: 已完成会话归属校验的固定前缀 Trace 视图

        Returns:
            已消费完整固定前缀的 Todo Group projector

        Raises:
            TaskTraceQueryTimeout: 等待容量或重放超过查询时限
            TracingError: 公共 Trace 分页读取失败
        """

        projector = TodoGroupProjector()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._limiter:
                    cursor: str | None = None
                    while True:
                        page = await trace.events(
                            cursor=cursor,
                            limit=_EVENT_PAGE_SIZE,
                        )
                        for event in page.items:
                            projector.consume(event)
                        cursor = page.next_cursor
                        if cursor is None:
                            return projector
        except TimeoutError:
            projector.close()
            raise TaskTraceQueryTimeout("任务轨迹查询超时") from None
        except BaseException:
            projector.close()
            raise


__all__ = ["TaskTraceQueryTimeout", "TodoGroupQueryExecutor"]
