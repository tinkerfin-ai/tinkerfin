# 多进程持久化与自定义扩展

[受限根目录与文件操作](rooted-filesystem.md) · [English](../../en/sandbox/persistence-and-extensions.md)

默认状态只存在当前进程。应用有多个 worker，或者进程重启后仍要恢复 Sandbox 绑定时，使用 SQLAlchemy 状态。

## SQLite：单机多进程

```bash
pip install "tinkerfin-sandbox[sqlite]"
```

```python
from tinkerfin_sandbox import (
    OpenSandboxManager,
    SQLAlchemyOpenSandboxState,
)


state = SQLAlchemyOpenSandboxState(
    url="sqlite+aiosqlite:////var/lib/app/opensandbox.db",
    namespace="production",
    lease_ttl=15.0,
    poll_interval=0.05,
    sqlite_retry_timeout=5.0,
)
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
)
```

## MySQL：多主机 worker

```bash
pip install "tinkerfin-sandbox[mysql]"
```

```python
state = SQLAlchemyOpenSandboxState(
    url="mysql+asyncmy://user:password@db/sandbox_state",
    namespace="production",
)
```

当前支持 MySQL 5.7 和 MySQL 8.x。MariaDB 不在已验证范围内。

### 状态参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `url` | 必填 | SQLAlchemy 异步连接 URL |
| `namespace` | `""` | 同一数据库中隔离不同部署 |
| `lease_ttl` | `15.0` | owner、warm slot 和 cleanup claim 的租约秒数 |
| `poll_interval` | `0.05` | 等待和重试的基础间隔 |
| `sqlite_retry_timeout` | `5.0` | SQLite 锁冲突的总重试预算 |

同一 namespace 的所有 worker 必须配置相同的 warm pool 大小。数据库账号在首次启动时需要建表和读写权限。

## 由基础设施提前建表

```python
from pathlib import Path
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema


schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
Path("opensandbox-schema.sql").write_text(schema.ddl, encoding="utf-8")
```

`dialect` 可以是 `mysql` 或 `sqlite`。返回值还包含 `table_names`，应用启动时仍会检查数据库结构是否匹配。

## 预热 Sandbox

```python
config = OpenSandboxConfig(warm_pool_size=2)
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
    warm_pool_size=2,
)
```

预热实例尚未属于具体 key。`get()` 消费 ready slot 时会原子地绑定它，不会先暴露无主实例。

## 如果创建后要初始化环境

给 client 传入异步 initializer：

```python
async def install_project(backend) -> None:
    await backend.aexecute(
        "git clone https://example.com/project.git /workspace/project"
    )


client = OpenSandboxClient(
    connection_config=connection_config,
    config=config,
    initializers=[install_project],
)
```

initializer 在新 Sandbox 可用后执行。它应当可取消、可观察，并在重复创建的新实例上得到一致结果；连接已有 Sandbox 时不会再次执行。

## 如果已有自己的状态存储

可以实现 `OpenSandboxState`，把绑定和租约保存到现有数据库。需要同时实现以下几组能力：

| 能力 | 方法 |
| --- | --- |
| 生命周期 | `start()`、`aclose()` |
| owner | `acquire_owner()`、`renew_owner()`、`bind_owner()`、`unbind_owner()`、`release_owner()`、`read_binding()` |
| warm pool | `claim_warm_slot()`、`renew_warm()`、`publish_warm()`、`release_warm()`、`consume_warm()` |
| cleanup | `enqueue_cleanup()`、`claim_cleanup()`、`renew_cleanup()`、`complete_cleanup()`、`release_cleanup()` |
| 关闭恢复 | `shutdown_sandbox_ids()` |

自定义状态必须有原子 claim、代际 fencing、租约续期和幂等释放。网络超时后结果不确定时，不得擅自销毁可能已经成为权威绑定的 Sandbox。

`InMemoryOpenSandboxState(namespace=...)` 可以作为行为参考，但它不适合跨进程共享。

下一篇：[Sandbox 使用参考](api-reference.md)。
