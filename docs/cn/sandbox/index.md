# Sandbox

[文档首页](../index.md) · [English](../../en/sandbox/index.md)

`tinkerfin-sandbox` 提供异步文件与命令、可复用隔离环境、持久绑定、预热容量、暂停、恢复和清理。

## 安装

```bash
pip install tinkerfin-sandbox
```

需要持久绑定时，安装 SQLAlchemy extra 和一种异步驱动：

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

PostgreSQL 使用 `asyncpg`，MySQL 使用 `asyncmy`。

## 在 AgentRuntime 中使用 Sandbox

两个范围都由应用决定：

- Runtime `namespace` 选择业务隔离范围
- `workspace_key` 选择该范围内哪些运行共享 Sandbox

key 可以按用户、session、项目或其他业务策略划分。

```python
from opensandbox.config import ConnectionConfig
from tinkerfin import TinkerFin
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxConfig, OpenSandboxManager

client = OpenSandboxClient(
    connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
    config=OpenSandboxConfig(workspace_root="/workspace"),
)

async with OpenSandboxManager(client=client) as sandboxes:
    runtime = (
        TinkerFin()
        .with_namespace("company-a")
        .build(
            model=model,
            backend=sandboxes.workspace("users/user-7"),
        )
    )
    result = await runtime.ainvoke(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
    )
```

`workspace(...)` 不执行 I/O。Runtime 只为已准入的运行创建或重连 Sandbox，并在清理时释放本次运行的句柄。一次运行结束不会销毁持久 Sandbox。

## 直接管理 Sandbox

应用需要在智能体运行之外操作环境时，使用 manager 方法：

```python
backend = await sandboxes.get("projects/project-1", namespace="company-a")
await backend.awrite("/notes.txt", "hello")
result = await backend.aexecute("python -m pytest", timeout=300)
```

| 任务 | 方法 |
| --- | --- |
| 打开或复用 | `get(key)` |
| 重连 | `reconnect(key)` |
| 替换 | `recreate(key)` |
| 清空 workspace 文件 | `reset(key)` |
| 暂停或恢复 | `pause(key)`、`resume(key)` |
| 销毁 | `destroy(key)` |
| 查看状态 | `get_details(key)` |
| 关闭本地资源 | `aclose()` |

## 持久化与所有权

`SQLAlchemyOpenSandboxState` 通过借用的 SQLAlchemy `AsyncEngine` 支持 SQLite、MySQL 和 PostgreSQL。应用负责创建和释放 Engine。State 保存绑定和生命周期协调状态，文件仍位于 Sandbox 或挂载卷中。

manager 拥有自己的 OpenSandbox client 和 State；调用方传入的 HTTP transport 仍由调用方管理。manager 关闭时，持久 State 保留远程 Sandbox，内存 State 销毁自己创建的实例。

文件工具被限制在 `workspace_root` 内，并拒绝逃逸路径和链接。Shell 命令是独立 Sandbox 能力，不受文件根目录限制。

## 后续阅读

- [生命周期](lifecycle.md)
- [文件与命令](rooted-filesystem.md)
- [持久 State 与扩展](persistence-and-extensions.md)
- [Sandbox API](api-reference.md)
