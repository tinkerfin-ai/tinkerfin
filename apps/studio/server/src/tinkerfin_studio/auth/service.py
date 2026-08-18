"""认证业务服务"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError

from tinkerfin_studio.api.errors import (
    AuthErrorCode,
    BusinessException,
    SystemException,
)
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.auth.passwords import verify_password
from tinkerfin_studio.auth.repository import (
    TokenRecord,
    TokenRepository,
    UserRepository,
)
from tinkerfin_studio.auth.types import RequestAuthState, UserContext

_DUMMY_PASSWORD_HASH = (
    "$pbkdf2-sha256$600000$AAAAAAAAAAAAAAAAAAAAAA$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """成功登录后返回给 HTTP 边界的结果"""

    access_token: str
    expires_in: int
    user: UserContext


class AuthService:
    """校验用户并管理分布式访问令牌"""

    def __init__(
        self,
        user_repository: UserRepository,
        token_repository: TokenRepository,
        *,
        token_expire_seconds: int,
    ) -> None:
        self._users = user_repository
        self._tokens = token_repository
        self._token_expire_seconds = token_expire_seconds

    async def login(self, username: str, password: str) -> LoginResult:
        """校验用户名密码并签发访问令牌"""

        user = await self._users.get_by_username(username)
        password_is_valid = await verify_password(
            password,
            _DUMMY_PASSWORD_HASH if user is None else user.password_hash,
        )
        if user is None or not password_is_valid:
            raise BusinessException(AuthErrorCode.BAD_CREDENTIALS)
        if user.disabled:
            raise BusinessException(AuthErrorCode.USER_DISABLED)

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._token_expire_seconds)
        try:
            await self._tokens.save(
                TokenRecord(token=token, user_id=user.id, expires_at=expires_at)
            )
        except RedisError as error:
            raise SystemException(AuthErrorCode.SERVICE_UNAVAILABLE) from error
        return LoginResult(
            access_token=token,
            expires_in=self._token_expire_seconds,
            user=self._context(user),
        )

    async def resolve_token(self, token: str | None) -> RequestAuthState:
        """解析令牌并返回完整鉴权状态"""

        if token is None:
            return RequestAuthState()
        try:
            record = await self._tokens.get(token)
        except RedisError as error:
            raise SystemException(AuthErrorCode.SERVICE_UNAVAILABLE) from error
        if record is None:
            return RequestAuthState(token=token, failure_reason="invalid_token")
        if record.revoked:
            return RequestAuthState(token=token, failure_reason="revoked_token")
        if record.is_expired:
            return RequestAuthState(token=token, failure_reason="expired_token")
        user = await self._users.get_by_id(record.user_id)
        if user is None:
            return RequestAuthState(token=token, failure_reason="user_not_found")
        if user.disabled:
            return RequestAuthState(token=token, failure_reason="disabled_user")
        return RequestAuthState(
            token=token,
            is_authenticated=True,
            user=self._context(user),
        )

    async def logout(self, token: str) -> None:
        """幂等撤销访问令牌"""

        try:
            await self._tokens.revoke(token)
        except RedisError as error:
            raise SystemException(AuthErrorCode.SERVICE_UNAVAILABLE) from error

    async def get_user(self, user_id: int) -> UserContext | None:
        """按用户 ID 返回安全上下文"""

        user = await self._users.get_by_id(user_id)
        return None if user is None else self._context(user)

    @staticmethod
    def _context(user: User) -> UserContext:
        return UserContext(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            roles=tuple(user.roles),
            disabled=user.disabled,
        )
