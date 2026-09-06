"""Studio 全局日志初始化测试"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_setup_logging_defaults_to_stdout_without_creating_files(
    tmp_path: Path,
) -> None:
    """容器缺省日志必须只写标准输出"""

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
    """显式开启文件日志时必须同时保留标准输出"""

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


@pytest.mark.parametrize("echo", (False, True))
@pytest.mark.parametrize("file_enabled", (False, True))
@pytest.mark.parametrize("level", ("DEBUG", "INFO", "ERROR"))
def test_database_sql_uses_host_handlers_once_per_output(
    tmp_path: Path, echo: bool, file_enabled: bool, level: str
) -> None:
    """SQL 开关独立控制可见性，每个宿主输出各记录一次且不影响执行"""

    program = """
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError

from tinkerfin_studio.config.logging import LoggingSettings, setup_logging
from tinkerfin_studio.infrastructure.database import Database

settings = LoggingSettings(
    level=sys.argv[3],
    file_enabled=json.loads(sys.argv[2]),
    file_path=Path("runtime/sql.log"),
)
executed = []
values = []
failures = []

def record_statement(connection, cursor, statement, parameters, context, many):
    executed.append(statement)

async def main():
    for _ in range(2):
        setup_logging(settings)
        async with Database(
            "sqlite+aiosqlite:///isolated.db", echo=json.loads(sys.argv[1])
        ) as database:
            event.listen(
                database.engine.sync_engine, "before_cursor_execute", record_statement
            )
            async with database.engine.connect() as connection:
                values.append(await connection.scalar(
                    text("SELECT :value AS logged_value"), {"value": 271828}
                ))
                try:
                    await connection.execute(text("SELECT * FROM missing_logging_table"))
                except SQLAlchemyError as error:
                    failures.append(type(error).__name__)
                    logging.getLogger("studio.test").error("query_failure_delivered")

asyncio.run(main())
print(json.dumps({"executed": executed, "values": values, "failures": failures}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            json.dumps(echo),
            json.dumps(file_enabled),
            level,
        ],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    statements = ("SELECT ? AS logged_value", "SELECT * FROM missing_logging_table")
    assert observed["values"] == [271828, 271828]
    assert observed["failures"] == ["OperationalError", "OperationalError"]
    assert observed["executed"] == list(statements) * 2
    outputs = [result.stdout]
    log_file = tmp_path / "runtime/sql.log"
    if file_enabled:
        outputs.append(log_file.read_text(encoding="utf-8"))
    else:
        assert not log_file.exists()
    for output in outputs:
        lines = output.splitlines()
        for statement in statements:
            sql_lines = [line for line in lines if line.endswith(statement)]
            assert len(sql_lines) == (2 if echo else 0)
            assert all("[INFO] [sqlalchemy.engine.Engine" in line for line in sql_lines)
        parameter_lines = [line for line in lines if line.endswith("(271828,)")]
        assert len(parameter_lines) == (2 if echo else 0)
        assert sum(line.endswith("query_failure_delivered") for line in lines) == 2
