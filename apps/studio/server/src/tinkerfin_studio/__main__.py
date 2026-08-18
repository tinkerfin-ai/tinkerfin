"""TinkerFin Studio 服务进程入口"""

import logging
from argparse import ArgumentParser, BooleanOptionalAction, Namespace
from collections.abc import Sequence

import uvicorn

from tinkerfin_studio.application import create_application
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.resources import build_lifespan

logger = logging.getLogger(__name__)

app = create_application(lifespan=build_lifespan())


def parse_args(args: Sequence[str] | None = None) -> Namespace:
    """解析服务监听与热重载参数"""

    parser = ArgumentParser(description="启动 TinkerFin Studio 服务")
    parser.add_argument("--host", default="127.0.0.1", help="服务监听地址")
    parser.add_argument("--port", type=int, default=8090, help="服务监听端口")
    parser.add_argument(
        "--reload",
        action=BooleanOptionalAction,
        default=True,
        help="是否开启开发热重载",
    )
    return parser.parse_args(args)


def main(args: Sequence[str] | None = None) -> None:
    """按命令行参数启动 HTTP 服务"""

    setup_logging()
    options = parse_args(args)
    logger.info("服务器已加载")
    uvicorn.run(
        "tinkerfin_studio.__main__:app",
        host=options.host,
        port=options.port,
        reload=options.reload,
        log_config=None,
    )


if __name__ == "__main__":
    main()
