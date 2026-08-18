"""全局 LangGraph Store 与 Redis checkpointer 生命周期"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from types import TracebackType
from typing import Self

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.store.base import BaseStore
from langgraph.store.mysql.asyncmy import AsyncMyStore
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from tinkerfin_studio.config.settings import DatabaseSettings, RedisSettings
from tinkerfin_studio.infrastructure.redis_client import create_redis_client


class AgentPersistence:
    """拥有 Deep Agents 共用 Store 与 checkpointer 的进程级资源"""

    def __init__(
        self,
        database: DatabaseSettings,
        redis: RedisSettings,
    ) -> None:
        self._database_settings = database
        self._redis_settings = redis
        self._store_resource: AbstractAsyncContextManager[AsyncMyStore] | None = None
        self._stack: AsyncExitStack | None = None
        self._store: AsyncMyStore | None = None
        self._checkpoint_redis: Redis | None = None
        self._checkpointer: AsyncRedisSaver | None = None

    @property
    def store(self) -> BaseStore:
        """返回生命周期内可用的 MySQL Store"""

        if self._store is None:
            raise RuntimeError("Agent Store 尚未启动")
        return self._store

    @property
    def checkpointer(self) -> AsyncRedisSaver:
        """返回生命周期内可用的 Redis checkpointer"""

        if self._checkpointer is None:
            raise RuntimeError("Agent checkpointer 尚未启动")
        return self._checkpointer

    async def __aenter__(self) -> Self:
        """初始化 Store pool、Redis client 和 checkpoint 索引"""

        if self._stack is not None:
            raise RuntimeError("Agent persistence 已经启动")
        database_url = make_url(self._database_settings.url)
        store_url = database_url.set(drivername="mysql").render_as_string(
            hide_password=False
        )
        stack = AsyncExitStack()
        store_resource = AsyncMyStore.from_conn_string(store_url)
        store = await stack.enter_async_context(store_resource)
        checkpoint_redis = create_redis_client(
            self._redis_settings,
            database=self._redis_settings.checkpoint_database,
        )
        checkpointer = AsyncRedisSaver(
            redis_client=checkpoint_redis,
            checkpoint_prefix=self._redis_settings.checkpoint_prefix,
            checkpoint_write_prefix=self._redis_settings.checkpoint_write_prefix,
        )
        try:
            await store.setup()
            await checkpointer.asetup()
        except BaseException:
            await checkpoint_redis.aclose()
            await stack.aclose()
            raise
        self._stack = stack
        self._store_resource = store_resource
        self._store = store
        self._checkpoint_redis = checkpoint_redis
        self._checkpointer = checkpointer
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭 Redis client 与 MySQL pool"""

        del exc_type, exc_value, traceback
        checkpoint_redis = self._checkpoint_redis
        stack = self._stack
        self._checkpointer = None
        self._checkpoint_redis = None
        self._store = None
        self._store_resource = None
        self._stack = None
        if checkpoint_redis is not None:
            await checkpoint_redis.aclose()
        if stack is not None:
            await stack.aclose()
