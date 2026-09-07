from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.infrastructure.database import Base, Database


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    """提供每个测试独占的 SQLite 数据库"""

    resource = Database(f"sqlite+aiosqlite:///{tmp_path / 'studio.db'}")
    async with resource:
        async with resource.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield resource


@pytest_asyncio.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """提供测试独占的异步 Session"""

    async with database.session() as value:
        yield value


@pytest_asyncio.fixture
async def attachments(database, tmp_path):
    """提供使用真实磁盘和业务仓储的附件服务"""
    from tinkerfin_studio.attachments.service import AttachmentService
    from tinkerfin_studio.attachments.storage import DiskAttachmentStorage

    return AttachmentService(database, DiskAttachmentStorage(tmp_path / "attachments"))
