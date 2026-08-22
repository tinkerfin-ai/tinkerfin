"""HTTP 请求依赖"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import BusinessException, GlobalErrorCode
from tinkerfin_studio.auth.repository import RedisTokenRepository, UserRepository
from tinkerfin_studio.auth.service import AuthService
from tinkerfin_studio.auth.types import AuthenticatedSession, UserContext
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.service import (
    ConversationCommandService,
    ConversationHistoryService,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import get_resources


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为一个 HTTP 请求借用独立异步数据库会话"""

    async with get_resources(request.app).database.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_auth_service(request: Request, session: SessionDep) -> AuthService:
    """按请求会话构造认证业务服务"""

    resources = get_resources(request.app)
    return AuthService(
        UserRepository(session),
        RedisTokenRepository(
            resources.redis,
            key_prefix=resources.settings.redis.auth_key_prefix,
        ),
        token_expire_seconds=resources.settings.auth_token_expire_seconds,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_raw_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str | None:
    """读取规范 Bearer token，不接受其他认证 scheme"""

    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.casefold() == "bearer" and token.strip() == token and token:
        return token
    return None


RawTokenDep = Annotated[str | None, Depends(get_raw_token)]


async def get_auth_session(
    token: RawTokenDep,
    auth_service: AuthServiceDep,
) -> AuthenticatedSession:
    """要求访问令牌对应一个尚未到期的当前会话"""

    auth_state = await auth_service.resolve_token(token)
    if (
        not auth_state.is_authenticated
        or auth_state.token is None
        or auth_state.user is None
        or auth_state.expires_at is None
    ):
        raise BusinessException(GlobalErrorCode.UNAUTHORIZED)
    return AuthenticatedSession(
        token=auth_state.token,
        expires_at=auth_state.expires_at,
        user=auth_state.user,
    )


AuthSessionDep = Annotated[AuthenticatedSession, Depends(get_auth_session)]


async def get_user_context(
    auth_session: AuthSessionDep,
) -> UserContext:
    """返回已通过固定会话校验的用户上下文"""

    return auth_session.user


UserContextDep = Annotated[UserContext, Depends(get_user_context)]


async def get_model_service(session: SessionDep) -> AgentModelService:
    """按请求会话构造模型目录服务"""

    return AgentModelService(AgentModelRepository(session))


ModelServiceDep = Annotated[AgentModelService, Depends(get_model_service)]


async def get_conversation_history_service(
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> ConversationHistoryService:
    """构造当前用户的会话历史查询服务"""

    return ConversationHistoryService(
        ConversationRepository(session),
        user_id=user.user_id,
        projector=get_resources(request.app).conversation_projector,
    )


ConversationHistoryDep = Annotated[
    ConversationHistoryService,
    Depends(get_conversation_history_service),
]


async def get_conversation_command_service(
    request: Request,
    session: SessionDep,
    user: UserContextDep,
) -> ConversationCommandService:
    """构造当前用户的会话命令服务"""

    return ConversationCommandService(
        ConversationRepository(session),
        user_id=user.user_id,
        resources=get_resources(request.app),
    )


ConversationCommandDep = Annotated[
    ConversationCommandService,
    Depends(get_conversation_command_service),
]
