"""FastAPI 生命周期拥有的外部资源"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from dataclasses import dataclass
from types import TracebackType
from typing import NoReturn, TypeVar, cast

import httpx
from ag_ui.core import BaseEvent
from fastapi import FastAPI
from opensandbox.config import ConnectionConfig
from redis.asyncio import Redis

from tinkerfin import TinkerFin
from tinkerfin.redis import RedisRunCoordinator
from tinkerfin_messaging import MessagingLimits, MessagingRetentionPolicy
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.messaging import MessageChannel, Messaging
from tinkerfin_messaging.redis import RedisBackend
from tinkerfin_sandbox.lifecycle.client import OpenSandboxClient
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_sandbox.lifecycle.sqlalchemy import SQLAlchemyOpenSandboxState
from tinkerfin_sandbox.models import OpenSandboxConfig
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.config.settings import Settings, get_settings
from tinkerfin_studio.conversation.coordinator import (
    ConversationTraceCoordinator,
)
from tinkerfin_studio.conversation.todo_groups import TodoGroupQueryExecutor
from tinkerfin_studio.health import ReadinessService
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.infrastructure.redis_client import create_redis_client
from tinkerfin_studio.infrastructure.sandbox_events import SandboxEventLogger
from tinkerfin_tracing import (
    CapturePolicy,
    SqlAlchemyTraceStore,
    Tracer,
)

logger = logging.getLogger(__name__)
_STUDIO_MESSAGING_LIMITS = MessagingLimits(
    max_message_payload_bytes=4 * 1024 * 1024,
)
_ResourceT = TypeVar("_ResourceT")


class _LifespanOutcome:
    """保存生命周期主体异常，供每个资源退出边界读取同一主因"""

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.traceback: TracebackType | None = None

    def capture(self, error: BaseException) -> None:
        self.error = error
        self.traceback = error.__traceback__


async def _enter_lifespan_context(
    stack: AsyncExitStack,
    outcome: _LifespanOutcome,
    resource: AbstractAsyncContextManager[_ResourceT],
) -> _ResourceT:
    """进入资源并保证退出时收到生命周期主体的原始异常"""

    entered = await resource.__aenter__()

    async def close(
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        del _exc_type, _exc_value, _traceback
        error = outcome.error
        return bool(
            await resource.__aexit__(
                None if error is None else type(error),
                error,
                outcome.traceback,
            )
        )

    stack.push_async_exit(close)
    return entered


def _raise_lifespan_outcome(
    outcome: _LifespanOutcome,
    cleanup_error: BaseException | None,
) -> NoReturn:
    """按主异常、进程控制与清理失败的固定优先级重新抛出"""

    primary = outcome.error
    if primary is not None:
        if (
            cleanup_error is not None
            and isinstance(primary, Exception)
            and not isinstance(cleanup_error, Exception)
        ):
            raise cleanup_error.with_traceback(cleanup_error.__traceback__) from primary
        raise primary.with_traceback(outcome.traceback) from cleanup_error
    assert cleanup_error is not None
    raise cleanup_error.with_traceback(cleanup_error.__traceback__)


async def _settle_lifespan_stack(
    stack: AsyncExitStack,
    outcome: _LifespanOutcome,
) -> None:
    """尝试全部退出回调，并在完成后恢复生命周期的权威异常"""

    cleanup_error: BaseException | None = None
    primary = outcome.error
    try:
        await stack.__aexit__(
            None if primary is None else type(primary),
            primary,
            outcome.traceback,
        )
    except BaseException as error:  # noqa: BLE001 - 清理完毕后统一决定主因
        cleanup_error = error
    if primary is not None or cleanup_error is not None:
        _raise_lifespan_outcome(outcome, cleanup_error)


@dataclass(frozen=True, slots=True)
class ApplicationResources:
    """请求处理期间借用的应用级资源"""

    settings: Settings
    database: Database
    redis_control: Redis
    redis_runtime: Redis
    agent_persistence: AgentPersistence
    tinkerfin: TinkerFin
    tracer: Tracer
    todo_group_query: TodoGroupQueryExecutor
    messaging: Messaging
    conversation_channel: MessageChannel[BaseEvent, BaseEvent]
    sandbox_manager: OpenSandboxManager[str]
    conversation_trace: ConversationTraceCoordinator
    readiness: ReadinessService


def build_lifespan():
    """构造数据库与两个 Redis 故障域的 FastAPI 生命周期"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        logger.info(
            "认证访问令牌固定有效期：%s 秒",
            settings.auth_token_expire_seconds,
        )
        database_settings = settings.database
        database = Database(
            database_settings.url,
            echo=database_settings.echo,
            pool_size=database_settings.pool_size,
            max_overflow=database_settings.max_overflow,
            pool_recycle=database_settings.pool_recycle,
        )
        stack = AsyncExitStack()
        outcome = _LifespanOutcome()
        resources_published = False
        try:
            await _enter_lifespan_context(stack, outcome, database)
            connection_budget = await database.verify_connection_budget(
                configured_budget=database_settings.connection_budget,
                management_reserve=database_settings.management_connection_reserve,
            )
            logger.info(
                "MySQL 连接预算已验证：共享池=%s，Agent Store=%s，预算=%s，管理保留=%s",
                connection_budget.sqlalchemy_pool_capacity,
                connection_budget.dedicated_agent_store_connections,
                connection_budget.configured_budget,
                connection_budget.management_reserve,
            )
            redis_control_settings = settings.redis_control
            redis_runtime_settings = settings.redis_runtime
            redis_control = create_redis_client(redis_control_settings)
            redis_runtime = create_redis_client(redis_runtime_settings)
            stack.push_async_callback(redis_control.aclose)
            stack.push_async_callback(redis_runtime.aclose)
            http_client = await _enter_lifespan_context(
                stack, outcome, httpx.AsyncClient(trust_env=False)
            )
            control_ready, runtime_ready = await asyncio.gather(
                cast(Awaitable[bool], redis_control.ping()),
                cast(Awaitable[bool], redis_runtime.ping()),
            )
            if not control_ready:
                raise RuntimeError("Redis Control PING 未返回成功")
            if not runtime_ready:
                raise RuntimeError("Redis Runtime PING 未返回成功")

            persistence = await _enter_lifespan_context(
                stack,
                outcome,
                AgentPersistence(settings.database, redis_runtime_settings),
            )
            run_coordinator = await _enter_lifespan_context(
                stack,
                outcome,
                RedisRunCoordinator.from_client(
                    redis_control,
                    key_resolver=lambda identity: identity.thread_id,
                    key_prefix=redis_control_settings.run_key_prefix,
                ),
            )
            trace_store = SqlAlchemyTraceStore(
                database.engine,
                namespace="tinkerfin-studio",
            )
            # Trace Store 借用业务 Engine 并在接收请求前校验唯一当前 Schema
            await trace_store.setup()
            tracer = Tracer(
                store=trace_store,
                capture_policy=CapturePolicy.public_history(
                    include_error_messages=True
                ),
            )
            todo_group_query = TodoGroupQueryExecutor()
            # 每个 Run 由框架打开独立 Trace session，写入失败会让 Agent fail-closed
            # Studio 授权保留有界错误摘要；Tracer 仍不接管共享 Engine，reasoning 默认省略
            tinkerfin = TinkerFin(
                checkpointer=persistence.checkpointer,
                run_coordinator=run_coordinator,
            ).observe(tracer)
            sandbox_settings = settings.sandbox
            sandbox_manager = await _enter_lifespan_context(
                stack,
                outcome,
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
                            # 用户工作区跨会话保留，由明确的清理操作结束生命周期
                            ttl=None,
                            workspace_root=sandbox_settings.workspace_root,
                            warm_pool_size=sandbox_settings.warm_pool_size,
                        ),
                    ),
                    key_resolver=lambda owner: owner,
                    # Sandbox State 借用业务 Engine；关闭 Manager 不会销毁宿主连接池
                    state=SQLAlchemyOpenSandboxState(
                        engine=database.engine,
                        namespace=sandbox_settings.state_namespace,
                    ),
                    warm_pool_size=sandbox_settings.warm_pool_size,
                    fail_on_startup_warmup_error=True,
                    # 已确认的沙箱与预热容量变化复用应用日志输出
                    observers=(SandboxEventLogger(),),
                ),
            )
            messaging_backend = RedisBackend(
                redis_runtime,
                key_prefix=redis_runtime_settings.messaging_key_prefix,
                limits=_STUDIO_MESSAGING_LIMITS,
                # Messaging 只承担短期断线续播；长期正文由 Trace 提供
                retention_policy=(
                    MessagingRetentionPolicy.disabled()
                    if settings.messaging_retention_seconds == 0
                    else MessagingRetentionPolicy.expire_after(
                        settings.messaging_retention_seconds
                    )
                ),
            )
            messaging = await _enter_lifespan_context(
                stack, outcome, Messaging(backend=messaging_backend)
            )
            channel = messaging.channel(
                name="studio-conversation-agui",
                codec=AgUiCodec(),
            )
            conversation_trace = ConversationTraceCoordinator(
                database=database,
                tracer=tracer,
                conversation_channel=channel,
            )
            stack.push_async_callback(conversation_trace.aclose)
            await conversation_trace.recover_preparing()
            application.state.resources = ApplicationResources(
                settings=settings,
                database=database,
                redis_control=redis_control,
                redis_runtime=redis_runtime,
                agent_persistence=persistence,
                tinkerfin=tinkerfin,
                tracer=tracer,
                todo_group_query=todo_group_query,
                messaging=messaging,
                conversation_channel=channel,
                sandbox_manager=sandbox_manager,
                conversation_trace=conversation_trace,
                readiness=ReadinessService(
                    database=database,
                    redis_control=redis_control,
                    redis_runtime=redis_runtime,
                    sandbox=settings.sandbox,
                    sandbox_ready=sandbox_manager.check_ready,
                    http_client=http_client,
                ),
            )
            resources_published = True
            yield
        except BaseException as error:  # noqa: BLE001 - 生命周期必须保留所有主因
            outcome.capture(error)
        finally:
            if resources_published:
                del application.state.resources
            await _settle_lifespan_stack(stack, outcome)

    return lifespan


def get_resources(application: FastAPI) -> ApplicationResources:
    """读取生命周期内已初始化的应用资源"""

    try:
        resources: ApplicationResources = application.state.resources
    except AttributeError as error:
        raise RuntimeError("应用资源尚未初始化") from error
    return resources
