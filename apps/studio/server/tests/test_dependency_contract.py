"""Studio 发行依赖必须与真实异步资源实现一致"""

from importlib.metadata import distribution


def test_studio_uses_only_the_asyncmy_langgraph_store_distribution() -> None:
    """Studio 只安装已验证的 asyncmy Store 集成"""

    requirements = set(distribution("tinkerfin-studio").requires or ())

    assert "tinkerfin-langgraph-mysql<0.9.0,>=0.1.0" in requirements
    assert all("langgraph-checkpoint-mysql" not in item for item in requirements)
    assert all("aiomysql" not in item for item in requirements)
    assert all("pymysql" not in item for item in requirements)
