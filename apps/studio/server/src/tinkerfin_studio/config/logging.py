"""Studio 日志系统初始化"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LoggingSettings(BaseSettings):
    """进程日志输出配置"""

    model_config = SettingsConfigDict(env_prefix="LOG_", extra="ignore")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file_enabled: bool = False
    file_path: Path = Path("logs/tinkerfin-studio.log")
    file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    file_backup_count: int = Field(default=3, ge=1)


def setup_logging(settings: LoggingSettings | None = None) -> None:
    """初始化标准输出和可选滚动文件日志"""

    resolved = settings or LoggingSettings()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if resolved.file_enabled:
        resolved.file_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.insert(
            0,
            RotatingFileHandler(
                resolved.file_path,
                mode="a",
                maxBytes=resolved.file_max_bytes,
                backupCount=resolved.file_backup_count,
                encoding="utf-8",
                delay=True,
            ),
        )
    logging.basicConfig(
        level=getattr(logging, resolved.level),
        format=(
            "%(asctime)s [%(levelname)s] [%(name)s] [pid:%(process)d] "
            "[thread:%(threadName)s] %(filename)s:%(lineno)d - "
            "%(funcName)s() - %(message)s"
        ),
        handlers=handlers,
        force=True,
    )
