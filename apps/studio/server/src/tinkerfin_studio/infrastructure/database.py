"""SQLAlchemy 异步数据库资源"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Studio ORM 实体声明基类"""


@dataclass(frozen=True, slots=True)
class MySQLConnectionBudget:
    """记录启动时已由数据库证明的连接容量边界"""

    sqlalchemy_pool_capacity: int
    dedicated_agent_store_connections: int
    configured_budget: int
    management_reserve: int
    server_max_connections: int


@dataclass(frozen=True, slots=True)
class _DatabaseResources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


class Database:
    """拥有异步 Engine 与 Session 工厂的应用生命周期资源"""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int | None = None,
        max_overflow: int | None = None,
        pool_recycle: int = 3600,
    ) -> None:
        self._url = url
        self._echo = echo
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_recycle = pool_recycle
        self._resources: _DatabaseResources | None = None

    @property
    def engine(self) -> AsyncEngine:
        """返回生命周期内可用的异步 Engine"""

        resources = self._resources
        if resources is None:
            raise RuntimeError("数据库尚未启动")
        return resources.engine

    async def __aenter__(self) -> Self:
        """创建 Engine 和 Session 工厂"""

        if self._resources is not None:
            raise RuntimeError("数据库已经启动")
        logging_name = "studio"
        # SQL 可见性由应用配置决定，输出沿用宿主处理器，避免 echo 自建重复输出
        logging.getLogger(f"sqlalchemy.engine.Engine.{logging_name}").setLevel(
            logging.INFO if self._echo else logging.WARNING
        )
        options: dict[str, object] = {
            "echo": False,
            "hide_parameters": True,
            "logging_name": logging_name,
            "pool_pre_ping": True,
            "pool_recycle": self._pool_recycle,
        }
        if not self._url.startswith("sqlite"):
            if self._pool_size is not None:
                options["pool_size"] = self._pool_size
            if self._max_overflow is not None:
                options["max_overflow"] = self._max_overflow
        engine = create_async_engine(self._url, **options)
        self._resources = _DatabaseResources(
            engine=engine,
            session_factory=async_sessionmaker(
                engine,
                expire_on_commit=False,
                autoflush=False,
            ),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭 Engine 且不吞掉调用域异常"""

        del exc_type, exc_value, traceback
        resources = self._resources
        self._resources = None
        if resources is not None:
            await resources.engine.dispose()

    def session(self) -> AsyncSession:
        """创建一个由调用方关闭的异步会话"""

        resources = self._resources
        if resources is None:
            raise RuntimeError("数据库尚未启动")
        return resources.session_factory()

    async def verify_connection_budget(
        self,
        *,
        configured_budget: int,
        management_reserve: int,
        dedicated_agent_store_connections: int = 1,
    ) -> MySQLConnectionBudget:
        """用 `@@max_connections` 验证共享池与独立 Store 连接上限

        Studio 业务、Trace 与 Sandbox State 共用同一个 SQLAlchemy Engine，因此只计算一次
        `pool_size + max_overflow`。锁定版本 `AsyncMyStore.from_conn_string()` 另持有一条
        asyncmy connection，必须单独计入。管理保留连接不能被应用预算占用。

        Args:
            configured_budget: Studio 进程允许占用的 MySQL 连接总上限
            management_reserve: 必须留给数据库管理与故障处理的连接数
            dedicated_agent_store_connections: SQLAlchemy 池外的 Agent Store 连接数

        Returns:
            已验证的数据库、共享池与独立连接容量证据

        Raises:
            RuntimeError: 配置预算不足或服务器无法同时容纳预算与管理保留量
        """

        if self._pool_size is None or self._max_overflow is None:
            raise RuntimeError("MySQL 连接预算要求显式配置 pool_size 与 max_overflow")
        pool_capacity = self._pool_size + self._max_overflow
        required = pool_capacity + dedicated_agent_store_connections
        if required > configured_budget:
            raise RuntimeError("MySQL 连接预算无法覆盖共享池与 Agent Store connection")
        async with self.engine.connect() as connection:
            server_limit = await connection.scalar(text("SELECT @@max_connections"))
        if not isinstance(server_limit, int) or server_limit < 1:
            raise RuntimeError("MySQL 未返回有效的 max_connections")
        if configured_budget + management_reserve > server_limit:
            raise RuntimeError("MySQL max_connections 无法容纳应用预算与管理保留量")
        return MySQLConnectionBudget(
            sqlalchemy_pool_capacity=pool_capacity,
            dedicated_agent_store_connections=dedicated_agent_store_connections,
            configured_budget=configured_budget,
            management_reserve=management_reserve,
            server_max_connections=server_limit,
        )
