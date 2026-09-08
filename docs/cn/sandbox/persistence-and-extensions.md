# 多进程持久化与自定义扩展

[受限根目录与文件操作](rooted-filesystem.md) · [English](../../en/sandbox/persistence-and-extensions.md)

默认状态只存在当前进程。应用有多个 worker，或者进程重启后仍要恢复 Sandbox 绑定时，使用 SQLAlchemy 状态。

State 保存绑定和租约，不保存容器文件。工作区需要保留到明确清理时，应同时使用持久 State 和
`OpenSandboxConfig(ttl=None)`；Manager 正常关闭后会保留绑定和远端实例。文件必须跨实例或
存储故障保留时，仍需要持久卷和备份策略。

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

取消调用时，State 会等待数据库结果读取完毕、连接归还连接池。COMMIT 开始前已收到取消请求的
写入会回滚；COMMIT 已发出时，则等待确定其成功或结果未知。必要收尾可能超过调用方的工作
时限，但不会串行化独立事务，也不会改变 SQLite 锁冲突的重试预算。

## 由基础设施提前建表

```python
from pathlib import Path
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema


schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
Path("opensandbox-schema.sql").write_text(schema.ddl, encoding="utf-8")
```

`dialect` 可以是 `mysql` 或 `sqlite`。返回值还包含 `table_names`。应用启动时仍会检查完整的表、字段、
主键与索引结构，包括精确的索引集合和 unique 标志。

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

## 准备工作区

给 client 传入异步 initializer：

```python
async def prepare_project(backend) -> None:
    result = await backend.aexecute(
        "mkdir -p /workspace/project /workspace/output"
    )
    if result.exit_code != 0:
        raise RuntimeError("Could not prepare workspace directories")


client = OpenSandboxClient(
    connection_config=connection_config,
    config=config,
    initializers=[prepare_project],
)
```

初始化函数会在创建及每次连接已有 Sandbox 后执行，必须幂等并保留已有工作区内容。I/O 使用异步
回调并传播取消；同步回调必须非阻塞。连接和初始化共用 Client 与恢复策略中较早的截止时间。
初始化失败会抛出 `OpenSandboxInitializationError`，不会触发重试或重建。回调和时限约束见
[使用参考](api-reference.md)。

## 如果已有自己的状态存储

可以实现 `OpenSandboxState`，把绑定和租约保存到现有数据库。需要同时实现以下几组能力：

| 能力 | 方法 |
| --- | --- |
| 生命周期 | `start()`、`aclose()` |
| owner | `acquire_owner()`、`renew_owner()`、`bind_owner()`、`unbind_owner()`、`release_owner()`、`read_binding()` |
| warm pool | `claim_warm_slot()`、`claim_ready_warm_slot()`、`renew_warm()`、`publish_warm()`、`discard_ready_warm_slot()`、`release_warm()`、`warm_pool_ready()`、`consume_warm()` |
| cleanup | `enqueue_cleanup()`、`claim_cleanup()`、`renew_cleanup()`、`complete_cleanup()`、`release_cleanup()` |
| 关闭恢复 | `shutdown_sandbox_ids()` |

自定义状态必须有原子 claim、代际 fencing、租约续期和幂等释放。ready slot 在远端检查期间必须保留
已发布 ID；确认 ID 不可用后，清空 slot 与加入 cleanup 队列必须是一次原子转换。网络超时后结果不确定
时，不得擅自销毁可能已经成为权威绑定的 Sandbox。

`InMemoryOpenSandboxState(namespace=...)` 可以作为行为参考，但它不适合跨进程共享。

下一篇：[Sandbox 使用参考](api-reference.md)。
