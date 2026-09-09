"""Studio 版本来源测试"""

from __future__ import annotations

import tinkerfin_studio
from tinkerfin_studio.application import create_application


def test_application_uses_the_package_version() -> None:
    """OpenAPI 版本必须与 Studio 包版本保持一致"""

    application = create_application(lifespan=None)

    assert application.version == tinkerfin_studio.__version__
