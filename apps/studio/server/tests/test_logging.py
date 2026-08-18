"""Studio 全局日志初始化测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_setup_logging_defaults_to_stdout_without_creating_files(
    tmp_path: Path,
) -> None:
    """容器缺省日志必须只写标准输出。"""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import logging",
                    "from tinkerfin_studio.config.logging import setup_logging",
                    "setup_logging()",
                    "logging.getLogger('studio.test').warning('双通道日志')",
                    "[handler.flush() for handler in logging.getLogger().handlers]",
                )
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "双通道日志" in result.stdout
    assert not (tmp_path / "logs").exists()


def test_setup_logging_writes_rotating_file_when_enabled(tmp_path: Path) -> None:
    """显式开启文件日志时必须同时保留标准输出。"""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import logging",
                    "from tinkerfin_studio.config.logging import setup_logging",
                    "setup_logging()",
                    "logging.getLogger('studio.test').warning('双通道日志')",
                    "[handler.flush() for handler in logging.getLogger().handlers]",
                )
            ),
        ],
        cwd=tmp_path,
        env={
            "PATH": str(Path(sys.executable).parent),
            "LOG_FILE_ENABLED": "true",
            "LOG_FILE_PATH": "runtime/studio.log",
            "LOG_FILE_MAX_BYTES": "1024",
            "LOG_FILE_BACKUP_COUNT": "2",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "双通道日志" in result.stdout
    assert "双通道日志" in (tmp_path / "runtime/studio.log").read_text(encoding="utf-8")
