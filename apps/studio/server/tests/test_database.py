from sqlalchemy import text

from tinkerfin_studio.infrastructure.database import Database


async def test_database_lifecycle_yields_isolated_async_sessions(tmp_path) -> None:
    """数据库资源应显式打开、关闭并提供原生异步会话"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'studio.db'}")

    async with database:
        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1

    try:
        database.session()
    except RuntimeError as error:
        assert "尚未启动" in str(error)
    else:  # pragma: no cover - 提供失败原因
        raise AssertionError("关闭后的数据库不应创建会话")
