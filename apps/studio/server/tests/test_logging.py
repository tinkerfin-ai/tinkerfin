"""Studio 全局日志初始化测试"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("file_enabled", (False, True))
def test_logging_outputs_and_cleanup(tmp_path: Path, file_enabled: bool) -> None:
    """控制台始终输出，可选文件写入后随生命周期退出刷新关闭"""
    program = """
import asyncio
import logging
import sys
from pathlib import Path
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import load_settings
async def main():
    async with setup_logging(load_settings(env_file=Path('config.env'))):
        logging.getLogger('studio.test').warning('双通道日志')
asyncio.run(main())
"""
    (tmp_path / "config.env").write_text(
        "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
        f"LOG_FILE_ENABLED={str(file_enabled).lower()}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("双通道日志") == 1
    if file_enabled:
        assert (tmp_path / "logs/studio.log").read_text().count("双通道日志") == 1
    else:
        assert not (tmp_path / "logs").exists()


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

from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings
from tinkerfin_studio.infrastructure.database import Database

settings = Settings(
    _env_file=None, database_url="mysql+asyncmy://studio:secret@db:3306/studio",
    log_level=sys.argv[3],
    log_file_enabled=json.loads(sys.argv[2]),
    log_file_path=Path("runtime/sql.log").resolve(),
)
executed = []
values = []
failures = []

def record_statement(connection, cursor, statement, parameters, context, many):
    executed.append(statement)

async def main():
    for _ in range(2):
        async with setup_logging(settings), Database(
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
        timeout=30,
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
        assert parameter_lines == []
        assert (
            "SQL parameters hidden" in output
            if echo
            else "SQL parameters hidden" not in output
        )
        assert sum(line.endswith("query_failure_delivered") for line in lines) == 2


def test_application_startup_failure_is_logged_before_cleanup(tmp_path):
    """应用资源初始化失败时，文件保留启动日志与原始错误原因"""
    program = r"""
import asyncio
import logging
import threading
from pathlib import Path
from fastapi import FastAPI
import tinkerfin_studio.resources as resources
from tinkerfin_studio.config.settings import Settings
settings = Settings(
    _env_file=None, database_url="mysql+asyncmy://studio:secret@db:3306/studio",
    log_file_enabled=True, log_file_path=Path('studio.log').resolve(),
)
resources.get_settings = lambda: settings
def fail_database(*args, **kwargs):
    raise RuntimeError("isolated_startup_failure")
resources.Database = fail_database
async def main():
    try:
        async with resources.build_lifespan()(FastAPI()):
            raise AssertionError("initialization must fail")
    except RuntimeError as error:
        assert str(error) == "isolated_startup_failure"
asyncio.run(main())
assert not any(t.name.endswith('(_monitor)') for t in threading.enumerate())
logging.warning('console-after-cleanup')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    content = (tmp_path / "studio.log").read_text()
    assert "服务器已加载" in content
    assert "服务生命周期失败" in content
    assert "RuntimeError: isolated_startup_failure" in content
    assert result.stdout.count("console-after-cleanup") == 1
    assert "console-after-cleanup" not in content


def test_file_logging_rejects_unwritable_destination(tmp_path):
    """无效的文件目录阻止初始化，且不遗留监听线程"""
    program = r"""
import asyncio
import threading
from pathlib import Path
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings
blocker = Path('not-a-directory').resolve()
blocker.write_text('occupied')
settings = Settings(
    _env_file=None, database_url="mysql+asyncmy://studio:secret@db:3306/studio",
    log_file_enabled=True, log_file_path=blocker / 'studio.log',
)
async def main():
    try:
        async with setup_logging(settings):
            raise AssertionError('unwritable directory must fail')
    except OSError:
        pass
asyncio.run(main())
assert not any(t.name.endswith('(_monitor)') for t in threading.enumerate())
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_queue_pressure_keeps_event_loop_responsive_and_reports_loss(tmp_path):
    """慢输出不阻塞事件循环，队列满可观测且取消等待已接收日志完成"""
    program = r"""
import asyncio
import io
import logging
import sys
import threading
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings

class SlowOutput(io.StringIO):
    def write(self, value):
        entered.set()
        assert release.wait(10), 'output was not released'
        return super().write(value)

entered, release = threading.Event(), threading.Event()
output = SlowOutput()
settings = Settings(_env_file=None, database_url='mysql+asyncmy://u:p@db/studio')

async def main():
    active = asyncio.Event()
    async def run():
        async with setup_logging(settings):
            logging.warning('first-record')
            while not entered.is_set():
                await asyncio.sleep(0.001)
            for i in range(5000):
                logging.warning('record-%d', i)
            active.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(active.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), 'cleanup must wait for the sink'
        task.cancel()
    finally:
        release.set()
    try:
        await task
        raise AssertionError('cancellation was lost')
    except asyncio.CancelledError:
        pass

previous, sys.stdout = sys.stdout, output
try:
    asyncio.run(main())
finally:
    sys.stdout = previous
text = output.getvalue()
assert 'first-record' in text
assert '日志队列已满，丢弃 904 条日志' in text, text[-500:]
assert text.count('record-') == 4096
assert not any(t.name.endswith('(_monitor)') for t in threading.enumerate())
print('pressure-and-cancellation-ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "pressure-and-cancellation-ok" in result.stdout


def test_processes_rotate_one_shared_log_without_losing_records(tmp_path):
    """独立进程并发写入同一文件，保留窗口内记录完整且不重复"""
    import os

    program = r"""
import asyncio
import logging
import sys
from pathlib import Path
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings
async def main():
    settings = Settings(
        _env_file=None, database_url='mysql+asyncmy://u:p@db/studio',
        log_file_enabled=True, log_file_path=Path('studio.log').resolve(),
        log_file_max_bytes=1024, log_file_backup_count=100,
    )
    async with setup_logging(settings):
        for i in range(50):
            logging.warning('unique:%s:%d', sys.argv[1], i)
            await asyncio.sleep(0.001)
asyncio.run(main())
"""
    processes = []
    try:
        for i in range(4):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", program, str(i)],
                    cwd=tmp_path,
                    env={"PATH": os.defpath},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            _, errors = process.communicate(timeout=30)
            assert process.returncode == 0, errors
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    files = list(tmp_path.glob("studio.log*"))
    assert len(files) > 1
    lines = [line for path in files for line in path.read_text().splitlines()]
    observed = [line.split(" - ")[-1] for line in lines]
    assert sorted(observed) == sorted(
        f"unique:{p}:{i}" for p in range(4) for i in range(50)
    )


@pytest.mark.parametrize("startup", ("module", "direct", "reload", "workers"))
def test_uvicorn_processes_initialize_and_replace_log_output(tmp_path, startup):
    """真实入口与 Uvicorn 子进程均初始化日志，热重载后的进程继续写入"""
    import os
    import socket
    import time
    import urllib.error
    import urllib.request

    # 隔离外部资源装配；保留实际 CLI、应用导入、Uvicorn 与日志生命周期
    (tmp_path / "sitecustomize.py").write_text("""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
import tinkerfin_studio.config.settings as config
config.get_settings = lambda: config.load_settings(env_file=None)
import tinkerfin_studio.resources as resources
@asynccontextmanager
async def lifespan(app):
    async with resources.setup_logging(await asyncio.to_thread(config.get_settings)):
        logging.warning('probe-start:%s', os.getpid())
        try:
            yield
        finally:
            logging.warning('probe-stop:%s', os.getpid())
resources.build_lifespan = lambda: lifespan
""")
    watched = tmp_path / "reload_trigger.py"
    watched.write_text("value = 0\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if startup in ("module", "reload"):
        command = [
            sys.executable,
            "-m",
            "tinkerfin_studio",
            "--reload" if startup == "reload" else "--no-reload",
        ]
    else:
        command = [sys.executable, "-m", "uvicorn", "tinkerfin_studio.__main__:app"]
        if startup == "workers":
            command += ["--workers", "2"]
        else:
            command += ["--no-access-log"]
    command += ["--host", "127.0.0.1", "--port", str(port)]
    path = tmp_path / "studio.log"
    output = tmp_path / "console.txt"
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(tmp_path),
        "DATABASE_URL": "mysql+asyncmy://u:p@isolated-db/studio",
        "LOG_FILE_ENABLED": "true",
        "LOG_FILE_PATH": str(path),
        "WATCHFILES_FORCE_POLLING": "true",
        "WATCHFILES_POLL_DELAY_MS": "100",
    }

    def starts():
        if not path.exists():
            return set()
        return {
            line.split("probe-start:")[-1]
            for line in path.read_text().splitlines()
            if "probe-start:" in line
        }

    def wait_until(predicate):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, output.read_text()
            if predicate():
                return
            time.sleep(0.05)
        pytest.fail(f"等待 {startup} 超时：{output.read_text()}")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def ready():
        try:
            with opener.open(
                f"http://127.0.0.1:{port}/health/live", timeout=0.5
            ) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False

    with output.open("w") as console:
        process = subprocess.Popen(
            command, cwd=tmp_path, env=env, stdout=console, stderr=subprocess.STDOUT
        )
        try:
            wait_until(lambda: len(starts()) >= (2 if startup == "workers" else 1))
            wait_until(ready)
            initial = starts()
            if startup == "reload":
                # 启动日志可能先于监视器建立文件基线，等待它进入持续监视
                time.sleep(0.5)
                watched.write_text("value = 1\n")
                wait_until(lambda: bool(starts() - initial))
                wait_until(ready)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail(f"服务关闭超时：{output.read_text()}")
    assert process.returncode in (0, -15), output.read_text()
    contents = path.read_text()
    for pid in starts():
        assert contents.count(f"probe-start:{pid}\n") == 1
        assert contents.count(f"probe-stop:{pid}\n") == 1
    if startup == "direct":
        assert "GET /health/live HTTP" not in contents
    else:
        assert "GET /health/live HTTP" in contents
    assert "Logging error" not in output.read_text()


@pytest.mark.parametrize("phase", ("opening", "draining"))
@pytest.mark.parametrize("close_fails", (False, True))
def test_cancellation_finishes_logging_cleanup_even_when_close_fails(
    tmp_path, phase, close_fails
):
    """初始化和排空期间的重复取消均完成清理，关闭错误不能覆盖取消"""
    program = r"""
import asyncio
import logging
import sys
import threading
from pathlib import Path
from concurrent_log_handler import ConcurrentRotatingFileHandler
import tinkerfin_studio.config.logging as config
from tinkerfin_studio.config.settings import Settings
phase, close_fails = sys.argv[1], sys.argv[2] == 'true'
entered, release, closed = threading.Event(), threading.Event(), threading.Event()

class CheckedHandler(ConcurrentRotatingFileHandler):
    failed = False
    def do_open(self, mode=None):
        if phase == 'opening':
            entered.set()
            assert release.wait(10)
        return super().do_open(mode)
    def do_write(self, message):
        if phase == 'draining':
            entered.set()
            assert release.wait(10)
        return super().do_write(message)
    def close(self):
        super().close()
        closed.set()
        if close_fails and not self.failed:
            self.failed = True
            raise OSError('isolated-close-failure')

config.ConcurrentRotatingFileHandler = CheckedHandler
settings = Settings(
    _env_file=None, database_url='mysql+asyncmy://u:p@db/studio',
    log_file_enabled=True, log_file_path=Path('studio.log').resolve(),
)
async def run():
    async with config.setup_logging(settings):
        logging.warning('accepted-record')
        await asyncio.Event().wait()

async def main():
    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
    finally:
        release.set()
    try:
        await task
        raise AssertionError('cancellation was lost')
    except asyncio.CancelledError as error:
        if close_fails:
            assert isinstance(error.__cause__, OSError)
            assert str(error.__cause__) == 'isolated-close-failure'
    assert closed.is_set()
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
asyncio.run(main())
assert not any(t.name.endswith('(_monitor)') for t in threading.enumerate())
if phase == 'draining':
    assert Path('studio.log').read_text().count('accepted-record') == 1
"""
    result = subprocess.run(
        [sys.executable, "-c", program, phase, str(close_fails).lower()],
        cwd=tmp_path,
        env={"PATH": str(Path(sys.executable).parent)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "never retrieved" not in result.stderr
