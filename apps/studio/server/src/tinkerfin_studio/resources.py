"""FastAPI 生命周期拥有的外部资源"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import cast

import httpx
from ag_ui.core import BaseEvent
from fastapi import FastAPI
from opensandbox.config import ConnectionConfig
from redis.asyncio import Redis

from tinkerfin.redis import RedisRunCoordinator
from tinkerfin.runtime import TinkerFin
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.backend import MessagingBackend
from tinkerfin_messaging.messaging import MessageChannel, Messaging
from tinkerfin_messaging.redis import RedisBackend
from tinkerfin_sandbox.lifecycle.client import OpenSandboxClient
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_sandbox.lifecycle.sqlalchemy import SQLAlchemyOpenSandboxState
from tinkerfin_sandbox.models import OpenSandboxConfig
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.config.settings import Settings, get_settings
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.health import ReadinessService
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.infrastructure.redis_client import create_redis_client


@dataclass(frozen=True, slots=True)
class ApplicationResources:
    """请求处理期间借用的应用级资源"""

    settings: Settings
    database: Database
    redis: Redis
    agent_persistence: AgentPersistence
    run_coordinator: RedisRunCoordinator[str]
    tinkerfin: TinkerFin[str]
    messaging_backend: MessagingBackend
    messaging: Messaging
    conversation_channel: MessageChannel[BaseEvent, BaseEvent]
    sandbox_manager: OpenSandboxManager[str]
    conversation_projector: ConversationProjectionCoordinator
    readiness: ReadinessService


def build_lifespan():
    """构造数据库与 Redis 的 FastAPI 生命周期"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        database_settings = settings.database
        database = Database(
            database_settings.url,
            echo=database_settings.echo,
            pool_size=database_settings.pool_size,
            max_overflow=database_settings.max_overflow,
            pool_recycle=database_settings.pool_recycle,
        )
        redis = create_redis_client(settings.redis)
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(database)
            stack.push_async_callback(redis.aclose)
            http_client = await stack.enter_async_context(httpx.AsyncClient())
            if not await cast(Awaitable[bool], redis.ping()):
                raise RuntimeError("Redis PING 未返回成功")

            persistence = await stack.enter_async_context(
                AgentPersistence(settings.database, settings.redis)
            )
            run_coordinator = await stack.enter_async_context(
                RedisRunCoordinator.from_client(
                    redis,
                    key_resolver=lambda principal: principal,
                    key_prefix=settings.redis.run_key_prefix,
                )
            )
            sandbox_settings = settings.sandbox
            sandbox_manager = await stack.enter_async_context(
                OpenSandboxManager[str](
                    client=OpenSandboxClient(
                        connection_config=ConnectionConfig(
                            domain=sandbox_settings.domain,
                            protocol=sandbox_settings.protocol,
                            api_key=(
                                None
                                if sandbox_settings.api_key is None
                                else sandbox_settings.api_key.get_secret_value()
                            ),
                        ),
                        config=OpenSandboxConfig(
                            workspace_root=sandbox_settings.workspace_root,
                            warm_pool_size=sandbox_settings.warm_pool_size,
                        ),
                    ),
                    key_resolver=lambda owner: owner,
                    state=SQLAlchemyOpenSandboxState(
                        url=settings.database.url,
                        namespace=sandbox_settings.state_namespace,
                    ),
                    warm_pool_size=sandbox_settings.warm_pool_size,
                    fail_on_startup_warmup_error=True,
                )
            )
            messaging_backend = RedisBackend(
                redis,
                key_prefix=settings.redis.messaging_key_prefix,
            )
            projector = ConversationProjectionCoordinator(
                database=database,
                backend=messaging_backend,
                channel="studio-conversation-agui",
            )
            stack.push_async_callback(projector.aclose)
            messaging = await stack.enter_async_context(
                Messaging(backend=messaging_backend)
            )
            channel = messaging.channel(
                name="studio-conversation-agui",
                codec=AgUiCodec(),
            )
            application.state.resources = ApplicationResources(
                settings=settings,
                database=database,
                redis=redis,
                agent_persistence=persistence,
                run_coordinator=run_coordinator,
                tinkerfin=TinkerFin(run_coordinator=run_coordinator),
                messaging_backend=messaging_backend,
                messaging=messaging,
                conversation_channel=channel,
                sandbox_manager=sandbox_manager,
                conversation_projector=projector,
                readiness=ReadinessService(
                    database=database,
                    redis=redis,
                    sandbox=settings.sandbox,
                    http_client=http_client,
                ),
            )
            try:
                yield
            finally:
                del application.state.resources
        finally:
            await stack.aclose()

    return lifespan


def get_resources(application: FastAPI) -> ApplicationResources:
    """读取生命周期内已初始化的应用资源"""

    try:
        resources: ApplicationResources = application.state.resources
    except AttributeError as error:
        raise RuntimeError("应用资源尚未初始化") from error
    return resources
