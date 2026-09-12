from pathlib import Path

import pytest
from sqlalchemy import text

from tinkerfin_sandbox import SQLAlchemyOpenSandboxState
from tinkerfin_studio.infrastructure.database import Database


async def test_database_lifecycle_yields_isolated_async_sessions(
    tmp_path: Path,
) -> None:
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


async def test_sandbox_state_close_keeps_shared_business_engine_usable(
    tmp_path: Path,
) -> None:
    """Sandbox State 关闭后 Studio 共享 Engine 必须继续提供业务会话"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}")
    async with database:
        state = SQLAlchemyOpenSandboxState(engine=database.engine)
        await state.start(warm_pool_size=0)
        await state.aclose()

        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.docker_integration
async def test_mysql_connection_budget_covers_the_shared_pool(
    mysql_sandbox_url: str,
) -> None:
    """所有持久化能力共用连接池，预算不重复计算"""

    database = Database(mysql_sandbox_url, pool_size=2, max_overflow=1)
    async with database:
        verified = await database.verify_connection_budget(
            configured_budget=3,
            management_reserve=1,
        )
        assert verified.sqlalchemy_pool_capacity == 3
        assert verified.server_max_connections >= 5

        with pytest.raises(RuntimeError, match="max_connections"):
            await database.verify_connection_budget(
                configured_budget=verified.server_max_connections,
                management_reserve=1,
            )
