# 运行协调与 Redis 租约

[事件流与 SSE](streams-and-sse.md) · [English](../../en/runtime/extensions.md)

需要限制相同业务身份并发执行时，为 TinkerFin factory 配置 run coordinator。该
coordinator 会作用于这个 factory 创建的每个 Runtime。

## 限制同一身份的并发运行

如果同一用户、项目或会话不能同时运行两个任务，可以配置 coordinator。

```python
from tinkerfin import Identity, InMemoryRunCoordinator, TinkerFin


coordinator = InMemoryRunCoordinator(
    key_resolver=lambda identity: identity.thread_id,
)
tinkerfin = TinkerFin(run_coordinator=coordinator)

identity = Identity(threadId="tenant-7/user-42", runId="run-1")
agent = tinkerfin.create_deep_agent(model=model, tools=tools)
runtime = agent.new(identity=identity)
stream = runtime.astream(graph_input)
```

每个 Runtime 都有 `Identity`。coordinator 接收同一个完整值，并在 Native 或 AG-UI
事件流的整个生命周期内持有协调作用域。

内存 coordinator 只协调当前进程。如果应用有多个进程，可以实现 `RunCoordinator`，把锁放到共享系统中：

```python
from contextlib import asynccontextmanager


class CustomRunCoordinator:
    @asynccontextmanager
    async def __call__(self, identity: Identity):
        lock = await acquire_lock(identity.thread_id)
        try:
            yield
        finally:
            await lock.release()
```

自定义实现需要保证取消时释放锁，并为锁等待设置合理超时。coordinator 只控制并发，不保存 Graph 状态；连续会话仍需要 checkpointer。

## 如果需要通用 Redis 租约锁

安装 Redis 集成：

```bash
pip install "tinkerfin[redis]"
```

```python
from tinkerfin.redis import RedisLeaseLock


lock = RedisLeaseLock.from_client(redis, key_prefix="my-app:locks")

async with lock:
    async with lock.hold("invoice-42") as lease:
        print(lease.fencing_token)
        await update_invoice()
```

也可以让锁自己创建 Redis client：

```python
lock = RedisLeaseLock.from_url(
    "redis://localhost:6379/0",
    key_prefix="my-app:locks",
)
```

| 构造入口 | 必填参数 | Redis client 由谁关闭 |
| --- | --- | --- |
| `from_client(client, ...)` | 异步 Redis client | 调用方；锁只借用 |
| `from_url(url, ...)` | Redis URL | `RedisLeaseLock` |

两个入口使用相同的可选参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `key_prefix` | `"tinkerfin:lease:"` | 隔离当前应用的锁 key；非空且不能有首尾空白 |
| `lease_ttl_seconds` | `30.0` | 一次租约的 Redis TTL，必须大于 0 |
| `renew_interval_seconds` | `None` | `None` 表示 TTL 的三分之一；显式值必须小于 TTL 的一半 |
| `wait_poll_seconds` | `0.1` | 锁被占用时再次尝试的间隔，必须大于 0 |

`hold(resource_key)` 等到该资源租约可用，并返回包含 `resource_key` 与 `fencing_token` 的不可变 `RedisLease`。等待可以取消，同一个锁实例可以同时管理不同资源 key。

锁会自动续期。续期超时、连接断开、结果不确定或 owner token 不匹配时，当前持锁任务会被取消；退出时会等待续期任务停止，再使用 owner token 安全释放。Redis 不可用时会直接失败，不会退化为无锁执行。

租约过期后，新持有者可能已经进入，而暂停的旧持有者仍可能恢复并尝试写外部存储。`fencing_token` 对同一资源的成功获取单调递增；如果外部存储必须拒绝旧写入，可以把它用于条件更新。只需要 Redis 互斥时可以不使用 fencing，是否增加数据库锁或其他一致性措施由应用决定。

关闭 `RedisLeaseLock` 会拒绝新作用域，并等待已有作用域完成清理。fencing 计数 key 会保留，以避免正常运行时 token 回退；清空或恢复 Redis 数据后，应用必须自行决定如何维持外部 fencing 单调性。

活跃租约、等待者和释放操作会通过同一个 Redis 连接池发送短命令，但不会在两次轮询或续期之间独占连接。连接池应覆盖峰值并发获取、续期、释放以及应用自己的 Redis 请求。Redis Cluster 不受支持。

## 观察器适合做什么

`on_part` 和 AG-UI 的 `on_event` 适合：

- 写入监控指标；
- 记录审计日志；
- 更新运行进度；
- 在事件交付前执行轻量校验。

观察器会影响主事件流。不要在其中做同步网络请求或无法取消的长任务。

下一篇：[Runtime 使用参考](api-reference.md)。
