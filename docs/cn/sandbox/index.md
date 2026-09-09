# Sandbox 入门

[文档首页](../index.md) · [English](../../en/sandbox/index.md)

`tinkerfin-sandbox` 把 OpenSandbox 接入 Deep Agents。Agent 可以在隔离环境中执行命令、读写文件，而不是直接操作应用服务器。

## 什么时候使用

- Agent 需要执行 Shell 命令；
- Agent 需要修改项目文件；
- 不同用户或项目需要相互隔离；
- Sandbox 断线后需要自动重连或替换；
- 多个应用进程需要共享 Sandbox 绑定。

## 安装

```bash
pip install tinkerfin-sandbox
```

还需要一个可访问的 OpenSandbox 服务。连接地址和 API key 可以显式配置，也可以按 OpenSandbox 的规则放在环境变量中。

## 第一个 Sandbox Agent

```python
from deepagents import create_deep_agent
from opensandbox.config import ConnectionConfig
from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
)


client = OpenSandboxClient(
    connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
    config=OpenSandboxConfig(workspace_root="/workspace"),
)
manager = OpenSandboxManager[str](
    client=client,
    key_resolver=lambda key: key,
)


async with manager:
    backend = await manager.get("tenant-1/user-7")
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=manager.build_agent_middleware(backend),
    )
```

顺序很重要：先从 manager 取得 backend，再用这个 backend 创建 Graph。

## key 表示谁的 Sandbox

TinkerFin 不规定 key 必须是用户、会话还是项目。由应用选择稳定范围：

```python
manager = OpenSandboxManager[tuple[int, int]](
    client=client,
    key_resolver=lambda key: f"org/{key[0]}/project/{key[1]}",
)
```

解析后相同的字符串会共享一个稳定 handle，并串行处理生命周期变化。不同 key 可以并发执行。

不要把密码、token 等秘密直接放进 key。远端 owner label 使用摘要，但 key 仍可能出现在应用日志和状态存储中。

## 资源由谁关闭

默认情况下，manager 管理 client 和 state 的生命周期。Graph 借用 `manager.get()` 返回的 backend，不单独关闭它。

```python
async with manager:
    backend = await manager.get(key)
    # 使用 backend
# manager 在这里等待操作完成并关闭本地资源
```

所有远程操作都使用异步方法。不要调用同步的 `execute()`、`read()` 或 `write()`；对应使用 `aexecute()`、`aread()` 和 `awrite()`。

## 下一步

- [Sandbox 生命周期](lifecycle.md)
- [受限根目录与文件操作](rooted-filesystem.md)
- [多进程持久化与自定义扩展](persistence-and-extensions.md)
- [Sandbox 使用参考](api-reference.md)

