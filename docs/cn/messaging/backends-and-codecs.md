# Redis、自定义 codec 和 backend

[取消、延迟创建与恢复](cancellation-and-recovery.md) · [English](../../en/messaging/backends-and-codecs.md)

默认 `MemoryBackend` 适合单进程开发。如果多个进程要共享事件、运行状态和取消请求，使用 Redis。

## 使用 Redis

```bash
pip install "tinkerfin-messaging[redis]"
```

```python
from redis.asyncio import Redis
from tinkerfin_messaging import (
    Messaging,
    MessagingLimits,
    MessagingRetentionPolicy,
    RedisBackend,
)


redis = Redis.from_url(
    "redis://localhost:6379/0",
    decode_responses=False,
)
backend = RedisBackend(
    redis,
    key_prefix="my-app:tinkerfin",
    producer_lease_seconds=15.0,
    generation_cleanup_retry_seconds=0.1,
    limits=MessagingLimits(),
    retention_policy=MessagingRetentionPolicy.expire_after(86_400),
)
messaging = Messaging(backend=backend)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `client` | 必填 | 异步 Redis client，必须返回 bytes |
| `key_prefix` | `tinkerfin-messaging` | 当前应用独占的 Redis key 前缀 |
| `producer_lease_seconds` | `15.0` | Redis 时钟上的生产者所有权持续秒数 |
| `generation_cleanup_retry_seconds` | `0.1` | generation 清理所有权的重试间隔秒数 |
| `limits` | `MessagingLimits()` | 单条、单线程和保留数据总量上限 |
| `retention_policy` | 关闭 | thread generation 终态后的重播窗口 |

Redis client 是调用方提供的资源，应用关闭时自行关闭。连接池容量要覆盖同时等待消息、等待取消和普通命令的连接数。

启用 retention 后，从终态结算时开始计时。active producer 不会过期，截止前的新 Run 会清除
计时。过期 generation 抛出 `StreamExpired`，显式 `after=0` 启动会创建下一个空 generation。
Redis 使用服务器时钟，在 Backend 操作第一次观察到截止点时执行可恢复的物理清理。
`delete_stream()` 仍是独立的显式 `StreamDeleted` 生命周期。

## 显式使用内置 codec

先安装对应 codec extra：

```bash
pip install "tinkerfin-messaging[agui]"
# 或：pip install "tinkerfin-messaging[native]"
```

```python
from tinkerfin_messaging import AgUiCodec


codec = AgUiCodec()
channel = messaging.channel(
    name="agent-events",
    codec=codec,
    renderer=codec,
)
```

| 安装范围 | API | 用途 |
| --- | --- | --- |
| `[agui]` | `AgUiCodec` | AG-UI 事件编码、解码和 SSE |
| `[native]` | `NativeStreamPartCodec` | canonical Native replay 编码、解码和 SSE |
| `[redis]` | `RedisBackend` | 多进程持久 backend |

TinkerFin 的规范事件流带有 codec 与 RunIdentity，因此 name-only channel 可以自动选择 codec 和
durable scope。Native Runtime source 还会通过 `MessageCodecInputSource` 转交 Driver-owned
`NativeStreamPart`，codec 不会再次解析 live 上游 Mapping。自定义 source 必须显式配置 codec，
并在调用时提供 RunIdentity。

RedisBackend 保存限额、每个 generation 的 Payload 计数，以及当前和上一个 owner 的成功
续租次数与 UTC 时间，用于可信故障取证。共享同一 channel 的 worker 必须使用相同的全部
限额和保留策略；共享同一 `key_prefix` 的 worker 必须使用相同的总限额。同一前缀下的所有
channel 位于一个 Redis Cluster hash slot，容量准入、写入和计数在同一事务中完成。
这些字段不会进入 `MessageEnvelope`。

默认上限为单条编码消息 16 MiB、checkpoint position 1 MiB、每个 thread generation
100,000 条消息与 1 GiB Payload，以及每个 MemoryBackend 实例或 Redis 前缀合计
1 GiB 字节和 100,000 条记录。通过 `MessagingLimits.max_total_bytes` 和
`max_total_records` 调整总限额。

总字节包括编码 Payload、每条消息保留的 checkpoint 证据，以及每个 Run 最新的 checkpoint。
checkpoint 按 position 和 UTF-8 消息 ID 的字节数计费；替换 Run 最新 checkpoint 时只计算差额。
每个 channel、thread、存活 generation、Run、消息和墓碑各计一条记录。这是逻辑存储限额，
不是 Python 或 Redis 实际分配内存的测量值。自定义 backend 通过 `messaging_settings`
提供相同的限额，并在修改数据前拒绝超额写入。

已提交消息的幂等重试不会重复计费。`MessagingQuotaExceeded` 标明耗尽的资源；满额时仍可
取消、结算和删除。清理释放消息和 Run 占用，并将 generation 记录转为墓碑；channel、thread
和墓碑仍占用记录配额。自动过期默认关闭，显式开启后，新写入会通过有界到期索引回收其他
已到期线程，无需再次访问原线程或启动后台任务；不会为腾出空间驱逐活跃或未到期历史。

## 自定义消息格式

如果要保存自己的对象，实现 `MessageCodec`：

```python
import json


class JsonEventCodec:
    codec_id = "my-app.event.v1"

    def encode(self, item: dict[str, object]) -> bytes:
        return json.dumps(item, separators=(",", ":")).encode()

    def decode(self, payload: bytes) -> dict[str, object]:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("event must be an object")
        return value
```

`codec_id` 是持久格式标识。已经有数据后不要在不兼容的情况下复用同一个 ID。

如果还要输出 SSE，实现 renderer：

```python
class JsonEventRenderer:
    def render(self, *, seq: int, payload: dict[str, object]) -> bytes:
        data = json.dumps(payload, separators=(",", ":"))
        return f"id: {seq}\nevent: custom\ndata: {data}\n\n".encode()
```

```python
channel = messaging.channel(
    name="custom-events",
    codec=JsonEventCodec(),
    renderer=JsonEventRenderer(),
)
```

## 自定义 source

一个 `MessageSource` 需要支持异步迭代和幂等关闭：

```python
class QueueSource:
    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is STOP:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self.closed = True
```

如果 source 能稳定声明自己的 codec profile，可以实现 `ProfiledMessageSource`；普通业务 source 通常直接给 channel 传 codec 更简单。

## 自定义 backend

只有需要接入其他持久化存储时才实现 `MessagingBackend`。Messaging 负责 producer
任务、取消、follow 循环、收尾、retention 判定和错误转换；Backend 作者只实现一个不可变设置
属性和六个存储操作：

| 扩展成员 | Backend 职责 |
| --- | --- |
| `messaging_settings` | 返回所有协作 worker 共享的 limits、retention、producer lease、续租和等待设置 |
| `prepare_messaging_storage()` | 幂等创建或校验唯一当前存储形态，不接管调用方注入的 client |
| `commit_messaging_transition(transition)` | 原子提交框架定义的一个 transition，并返回该提交的准确持久化结果 |
| `load_messaging_state(query)` | 返回指定 channel、generation、run 和消息证据的存储时钟一致有界快照 |
| `read_committed_messages(query)` | 返回精确 generation 的升序有界页和变化游标；`stop_at_run_terminal=True` 时，必须在同一原子视图中返回 `run_state` |
| `wait_for_messaging_change(wait)` | 等待消息或控制状态可能变化；允许超时空唤醒，取消时必须释放订阅或独占连接 |
| `purge_stream_generation(purge)` | 对已经封闭清理的 generation 幂等删除一个有界批次，不得删除共享 control 或 tombstone |

`MessagingTransition`、`MessagingStateSnapshot` 和 `MessagingStorageEffect` 是不可变的
存储中立值。事务型 Backend 在事务内加载所需状态，调用
`resolve_messaging_transition()`，再原子应用返回的 effect。只有已经证明的乐观并发冲突可以
安全重试；取消或结果不确定的外部提交不得被静默当作成功。

`begin_generation_cleanup` 可以返回一个不透明、生命周期有界的 `cleanup_token`。
Messaging 不解析也不持久化该 token，只会把它原样、串行传给同一精确 generation 和实际清理原因的
`purge_stream_generation()` 与 `finish_generation_cleanup`。Backend 返回 token 时，必须保证其外部
lease 有界、允许幂等重试，并在调用取消或进程退出后允许新的清理尝试接管。

数据库和网络调用必须使用原生异步接口。注入的 client、连接池和关闭过程由宿主拥有。
`wait_for_messaging_change()` 必须先释放自身等待资源，再继续传播 `CancelledError`。
`purge_stream_generation()` 必须有界。不同清理尝试可以并发，但 generation 与 token fence 必须保证
任何一方都不会删除其他 generation。

使用空的隔离 namespace 运行公共契约验证：

```python
from contextlib import asynccontextmanager

from tinkerfin_messaging.testing import verify_messaging_backend


@asynccontextmanager
async def open_backend():
    backend = MyBackend(client, namespace="contract-test")
    try:
        yield backend
    finally:
        await delete_contract_test_namespace()


await verify_messaging_backend(open_backend)
```

验证器覆盖受支持的 Messaging 行为。分布式实现还必须使用真实存储验证并发竞争、lease 过期、
进程丢失、不确定传输结果和存储专用的清理恢复。

下一篇：[Messaging 使用参考](api-reference.md)。
