"""应用外部资源配置与读取入口"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_SECRET_FILE_TARGETS = {
    "database_url_file": "database_url",
    "redis_password_file": "redis_password",
    "open_sandbox_api_key_file": "open_sandbox_api_key",
    "tavily_api_key_file": "tavily_api_key",
}


def _apply_secret_files(values: dict[str, str]) -> None:
    """用容器 Secret 文件覆盖对应的普通配置值"""

    for file_key, target_key in _SECRET_FILE_TARGETS.items():
        raw_path = values.pop(file_key, None)
        if raw_path is None:
            continue
        path = Path(raw_path)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(f"{file_key.upper()} 无法读取: {path}") from error
        if not value:
            raise ValueError(f"{file_key.upper()} 内容不能为空")
        values[target_key] = value


class DatabaseSettings(BaseModel):
    """异步 SQLAlchemy 数据库连接配置"""

    model_config = ConfigDict(frozen=True)

    url: str = Field(
        min_length=1, repr=False, description="包含凭据的异步数据库连接地址"
    )
    echo: bool = Field(default=False, description="是否输出 SQL 日志")
    pool_size: int = Field(default=10, ge=1, description="连接池常驻连接数")
    max_overflow: int = Field(default=10, ge=0, description="连接池临时连接上限")
    pool_recycle: int = Field(
        default=3600, ge=-1, description="连接回收秒数，-1 表示关闭"
    )


class RedisSettings(BaseModel):
    """认证、协调、Messaging 与 checkpoint 使用的 Redis 配置"""

    model_config = ConfigDict(frozen=True)

    host: str = Field(min_length=1, description="Redis 主机")
    port: int = Field(ge=1, le=65535, description="Redis 端口")
    password: SecretStr | None = Field(
        default=None, repr=False, description="Redis 密码"
    )
    database: int = Field(ge=0, description="普通 Redis 逻辑库")
    checkpoint_database: int = Field(
        ge=0, le=0, description="支持 RediSearch 的 checkpoint 逻辑库"
    )
    max_connections: int = Field(default=80, ge=8, description="连接池最大连接数")
    socket_timeout_seconds: float = Field(
        default=10.0, gt=0, description="单次 Redis 命令超时秒数"
    )
    messaging_key_prefix: str = Field(min_length=1, description="Messaging 独占键前缀")
    run_key_prefix: str = Field(min_length=1, description="分布式运行协调键前缀")
    auth_key_prefix: str = Field(min_length=1, description="认证令牌键前缀")
    checkpoint_prefix: str = Field(min_length=1, description="checkpoint 记录键前缀")
    checkpoint_write_prefix: str = Field(
        min_length=1, description="checkpoint 写入键前缀"
    )


class SandboxSettings(BaseModel):
    """OpenSandbox 控制面和持久化分配配置"""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(min_length=1, description="OpenSandbox 控制面域名与端口")
    protocol: Literal["http", "https"] = Field(description="OpenSandbox 连接协议")
    api_key: SecretStr | None = Field(
        default=None, repr=False, description="OpenSandbox API 密钥"
    )
    warm_pool_size: int = Field(ge=0, description="全局预热 Sandbox 数量")
    workspace_root: str = Field(
        default="/workspace", description="Agent 文件系统虚拟根目录"
    )
    state_namespace: str = Field(
        min_length=1, max_length=64, description="Sandbox State 数据库命名空间"
    )


class Settings(BaseSettings):
    """Studio 服务进程配置"""

    model_config = SettingsConfigDict(
        env_file=_DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        min_length=1, repr=False, description="异步 MySQL 连接地址"
    )
    database_echo: bool = Field(default=False, description="是否输出 SQL 日志")
    database_pool_size: int = Field(default=10, ge=1, description="数据库常驻连接数")
    database_max_overflow: int = Field(default=10, ge=0, description="数据库临时连接数")
    database_pool_recycle: int = Field(
        default=3600, ge=-1, description="数据库连接回收秒数"
    )

    redis_host: str = Field(default="127.0.0.1", min_length=1, description="Redis 主机")
    redis_port: int = Field(default=6379, ge=1, le=65535, description="Redis 端口")
    redis_password: SecretStr | None = Field(
        default=None, repr=False, description="Redis 密码"
    )
    redis_db: int = Field(default=0, ge=0, description="普通 Redis 逻辑库")
    redis_checkpoint_db: int = Field(
        default=0, ge=0, le=0, description="checkpoint Redis 逻辑库"
    )
    redis_max_connections: int = Field(default=80, ge=8, description="Redis 最大连接数")
    redis_socket_timeout_seconds: float = Field(
        default=10.0, gt=0, description="Redis 命令超时秒数"
    )
    redis_messaging_key_prefix: str = Field(
        default="tinkerfin:studio:messaging",
        min_length=1,
        description="Messaging 键前缀",
    )
    redis_run_key_prefix: str = Field(
        default="tinkerfin:studio:run", min_length=1, description="运行协调键前缀"
    )
    redis_auth_key_prefix: str = Field(
        default="tinkerfin:studio:auth", min_length=1, description="认证键前缀"
    )
    redis_checkpoint_prefix: str = Field(
        default="tinkerfin:studio:checkpoint",
        min_length=1,
        description="checkpoint 键前缀",
    )
    redis_checkpoint_write_prefix: str = Field(
        default="tinkerfin:studio:checkpoint_write",
        min_length=1,
        description="checkpoint 写入键前缀",
    )

    open_sandbox_domain: str = Field(
        default="127.0.0.1:8091", min_length=1, description="OpenSandbox 域名与端口"
    )
    open_sandbox_protocol: Literal["http", "https"] = Field(
        default="http", description="OpenSandbox 连接协议"
    )
    open_sandbox_api_key: SecretStr | None = Field(
        default=None, repr=False, description="OpenSandbox API 密钥"
    )
    open_sandbox_warm_pool_size: int = Field(
        default=1, ge=0, description="全局预热 Sandbox 数量"
    )
    open_sandbox_workspace_root: str = Field(
        default="/workspace", min_length=1, description="Agent 文件系统虚拟根目录"
    )
    open_sandbox_state_namespace: str = Field(
        default="tinkerfin-studio",
        min_length=1,
        max_length=64,
        description="Sandbox State 命名空间",
    )

    auth_token_expire_seconds: int = Field(
        default=1800, ge=60, description="访问令牌有效秒数"
    )
    tavily_api_key: SecretStr | None = Field(
        default=None, repr=False, description="Tavily API 密钥"
    )

    @model_validator(mode="after")
    def validate_database_driver(self) -> Settings:
        """拒绝会在异步请求链中引入阻塞的数据库驱动"""

        if make_url(self.database_url).drivername != "mysql+asyncmy":
            raise ValueError("DATABASE_URL 必须使用 mysql+asyncmy 驱动")
        return self

    @property
    def database(self) -> DatabaseSettings:
        """返回数据库资源使用的分组配置"""

        return DatabaseSettings(
            url=self.database_url,
            echo=self.database_echo,
            pool_size=self.database_pool_size,
            max_overflow=self.database_max_overflow,
            pool_recycle=self.database_pool_recycle,
        )

    @property
    def redis(self) -> RedisSettings:
        """返回 Redis 资源使用的分组配置"""

        return RedisSettings(
            host=self.redis_host,
            port=self.redis_port,
            password=self.redis_password,
            database=self.redis_db,
            checkpoint_database=self.redis_checkpoint_db,
            max_connections=self.redis_max_connections,
            socket_timeout_seconds=self.redis_socket_timeout_seconds,
            messaging_key_prefix=self.redis_messaging_key_prefix,
            run_key_prefix=self.redis_run_key_prefix,
            auth_key_prefix=self.redis_auth_key_prefix,
            checkpoint_prefix=self.redis_checkpoint_prefix,
            checkpoint_write_prefix=self.redis_checkpoint_write_prefix,
        )

    @property
    def sandbox(self) -> SandboxSettings:
        """返回 OpenSandbox 资源使用的分组配置"""

        return SandboxSettings(
            domain=self.open_sandbox_domain,
            protocol=self.open_sandbox_protocol,
            api_key=self.open_sandbox_api_key,
            warm_pool_size=self.open_sandbox_warm_pool_size,
            workspace_root=self.open_sandbox_workspace_root,
            state_namespace=self.open_sandbox_state_namespace,
        )


@lru_cache
def get_settings() -> Settings:
    """读取并缓存真实运行配置"""

    return load_settings()


def load_settings(*, env_file: str | Path | None = _DEFAULT_ENV_FILE) -> Settings:
    """从指定环境文件读取配置，供 CLI 与隔离测试复用"""

    values: dict[str, str] = {}
    if env_file is not None:
        values.update(
            {
                key.lower(): value
                for key, value in dotenv_values(env_file).items()
                if value is not None
            }
        )
    values.update(
        {
            key.lower(): value
            for key, value in os.environ.items()
            if key.lower() in Settings.model_fields
            or key.lower() in _SECRET_FILE_TARGETS
        }
    )
    _apply_secret_files(values)
    return Settings.model_validate(values)
