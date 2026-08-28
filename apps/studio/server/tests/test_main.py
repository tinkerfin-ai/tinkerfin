import logging

import tinkerfin_studio.__main__ as server_entrypoint
from tinkerfin_studio.__main__ import parse_args


def test_server_arguments_keep_local_safe_defaults() -> None:
    """命令行缺省应仅监听本机并开启开发热重载"""

    options = parse_args([])

    assert options.host == "127.0.0.1"
    assert options.port == 8090
    assert options.reload is True


def test_server_arguments_allow_container_runtime_values() -> None:
    """部署入口应支持显式监听地址、端口和关闭热重载"""

    options = parse_args(["--host", "0.0.0.0", "--port", "9000", "--no-reload"])

    assert options.host == "0.0.0.0"
    assert options.port == 9000
    assert options.reload is False


def test_main_initializes_logging_before_starting_server(tmp_path, monkeypatch) -> None:
    """服务入口必须在启动 Uvicorn 前写出进程加载日志"""

    root_logger = logging.getLogger()
    previous_handlers = list(root_logger.handlers)
    previous_level = root_logger.level
    for handler in previous_handlers:
        root_logger.removeHandler(handler)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_FILE_ENABLED", "true")
    monkeypatch.setattr(
        server_entrypoint.uvicorn,
        "run",
        lambda *_args, **_kwargs: None,
    )

    try:
        server_entrypoint.main(["--no-reload"])
        for handler in root_logger.handlers:
            handler.flush()
    finally:
        for handler in list(root_logger.handlers):
            handler.close()
            root_logger.removeHandler(handler)
        for handler in previous_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(previous_level)

    log_file = tmp_path / "logs/tinkerfin-studio.log"
    assert "服务器已加载" in log_file.read_text(encoding="utf-8")
