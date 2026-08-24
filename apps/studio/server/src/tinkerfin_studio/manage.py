"""用户、模型和数据配置管理命令"""

from __future__ import annotations

import asyncio
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from getpass import getpass

from pydantic import SecretStr

from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.passwords import hash_password
from tinkerfin_studio.auth.repository import UserRepository
from tinkerfin_studio.config.settings import get_settings
from tinkerfin_studio.conversation.command_migration import (
    migrate_forwarded_commands,
)
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelWrite
from tinkerfin_studio.models.service import AgentModelService


def parse_args(args: Sequence[str] | None = None) -> Namespace:
    """解析用户和模型管理子命令"""

    parser = ArgumentParser(description="管理 TinkerFin Studio 数据")
    resources = parser.add_subparsers(dest="resource", required=True)

    user = resources.add_parser("user", help="管理用户")
    user_commands = user.add_subparsers(dest="command", required=True)
    create_user = user_commands.add_parser("create", help="创建用户")
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--display-name", required=True)
    create_user.add_argument("--password")
    create_user.add_argument("--role", action="append", default=[])

    model = resources.add_parser("model", help="管理模型")
    model_commands = model.add_subparsers(dest="command", required=True)
    upsert_model = model_commands.add_parser("upsert", help="新增或更新模型")
    upsert_model.add_argument("--model-id", required=True)
    upsert_model.add_argument("--display-name", required=True)
    upsert_model.add_argument(
        "--provider", choices=("deepseek", "openai"), required=True
    )
    upsert_model.add_argument("--model-name", required=True)
    upsert_model.add_argument("--base-url", required=True)
    upsert_model.add_argument("--api-key")
    upsert_model.add_argument("--reasoning", action="store_true")
    upsert_model.add_argument("--disabled", action="store_true")
    upsert_model.add_argument("--default", dest="is_default", action="store_true")
    upsert_model.add_argument("--sort-order", type=int, default=0)
    model_commands.add_parser("list", help="列出安全模型目录")

    data = resources.add_parser("data", help="管理当前部署的数据契约")
    data_commands = data.add_subparsers(dest="command", required=True)
    migrate_commands = data_commands.add_parser(
        "migrate-forwarded-commands",
        help="把 forwardedProps.mode 转换为 command.plan",
    )
    migrate_commands.add_argument(
        "--apply",
        action="store_true",
        help="写入已校验的转换；缺省只执行 dry-run",
    )
    return parser.parse_args(args)


async def _run(options: Namespace) -> None:
    settings = get_settings()
    database_settings = settings.database
    database = Database(
        database_settings.url,
        echo=database_settings.echo,
        pool_size=database_settings.pool_size,
        max_overflow=database_settings.max_overflow,
        pool_recycle=database_settings.pool_recycle,
    )
    async with database, database.session() as session:
        if options.resource == "user":
            repository = UserRepository(session)
            if await repository.get_by_username(options.username) is not None:
                raise ValueError(f"用户已存在: {options.username}")
            password = options.password or getpass("密码: ")
            repository.add(
                User(
                    username=options.username,
                    display_name=options.display_name,
                    password_hash=await hash_password(password),
                    roles=list(options.role),
                    disabled=False,
                )
            )
            await session.commit()
            print(f"已创建用户: {options.username}")
            return

        if options.resource == "data":
            result = await migrate_forwarded_commands(session, apply=options.apply)
            if options.apply:
                await session.commit()
            else:
                await session.rollback()
            action = "已迁移" if options.apply else "可迁移"
            print(
                f"{action} run={result.runs} event={result.events} "
                f"active_run={result.active_runs}"
            )
            return

        service = AgentModelService(AgentModelRepository(session))
        if options.command == "list":
            catalog = await service.list_catalog()
            for item in catalog.items:
                marker = "*" if item.is_default else " "
                print(f"{marker} {item.model_id}\t{item.display_name}")
            return
        api_key = options.api_key or getpass("模型 API key: ")
        await service.upsert(
            AgentModelWrite(
                model_id=options.model_id,
                display_name=options.display_name,
                provider=options.provider,
                model_name=options.model_name,
                base_url=options.base_url,
                api_key=SecretStr(api_key),
                reasoning_enabled=options.reasoning,
                enabled=not options.disabled,
                is_default=options.is_default,
                sort_order=options.sort_order,
            )
        )
        print(f"已保存模型: {options.model_id}")


def main(args: Sequence[str] | None = None) -> None:
    """执行管理命令"""

    asyncio.run(_run(parse_args(args)))


if __name__ == "__main__":
    main()
