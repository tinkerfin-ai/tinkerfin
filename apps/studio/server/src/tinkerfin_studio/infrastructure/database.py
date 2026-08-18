"""SQLAlchemy 异步数据库资源"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Studio ORM 实体声明基类"""


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
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        """返回生命周期内可用的异步 Engine"""

        if self._engine is None:
            raise RuntimeError("数据库尚未启动")
        return self._engine

    async def __aenter__(self) -> Self:
        """创建 Engine 和 Session 工厂"""

        if self._engine is not None:
            raise RuntimeError("数据库已经启动")
        options: dict[str, object] = {
            "echo": self._echo,
            "pool_pre_ping": True,
            "pool_recycle": self._pool_recycle,
        }
        if not self._url.startswith("sqlite"):
            if self._pool_size is not None:
                options["pool_size"] = self._pool_size
            if self._max_overflow is not None:
                options["max_overflow"] = self._max_overflow
        engine = create_async_engine(self._url, **options)
        self._engine = engine
        self._session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
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
        engine = self._engine
        self._session_factory = None
        self._engine = None
        if engine is not None:
            await engine.dispose()

    def session(self) -> AsyncSession:
        """创建一个由调用方关闭的异步会话"""

        factory = self._session_factory
        if factory is None:
            raise RuntimeError("数据库尚未启动")
        return factory()
