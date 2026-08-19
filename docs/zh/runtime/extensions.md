# 自定义事件源与并发协调

[事件流与 SSE](streams-and-sse.md) · [English](../../en/runtime/extensions.md)

大多数 Deep Agents 应用只需要 `create_deep_agent()`。如果你已经有自己的异步数据源，或者需要限制同一业务身份的并发运行，可以使用本页的能力。

## 运行自己的异步事件源

事件源必须是一个每次调用都会返回新异步迭代器的函数。

```python
import asyncio
from collections.abc import AsyncIterator

from tinkerfin import TinkerFin


async def source() -> AsyncIterator[dict[str, object]]:
    yield {"step": 1, "message": "started"}
    await asyncio.sleep(0.1)
    yield {"step": 2, "message": "finished"}


run = TinkerFin().run(source)
stream = run.astream()

async for item in stream:
    print(item)
```

### `TinkerFin.run()` 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `source_factory` | 必填 | 创建异步迭代器的函数，或通过 `AgUiNativeStreamConfig.bind()` 得到的调用对象 |
| `principal` | `None` | 协调运行时使用的业务身份 |
| `on_part` | `None` | 每条数据交付前执行的观察函数 |

不要把已经开始消费的同一个异步生成器重复传给多个运行。

## 把原生 v2 数据转换成 AG-UI

如果你的事件源就是 LangGraph v2 数据，可以先声明它遵循 AG-UI 所需的模式：

```python
from tinkerfin import AgUiNativeStreamConfig, TinkerFin


invocation = AgUiNativeStreamConfig().bind(
    graph.astream,
    graph_input,
    config,
)
run = TinkerFin().run(invocation)
events = run.astream_agui(run_input=run_input)
```

`AgUiNativeStreamConfig` 默认要求 `messages`、`tasks`、`values`、`version="v2"` 和 `subgraphs=True`。`extra_modes` 可以增加 `updates`、`checkpoints`、`debug` 或 `custom`：

```python
config = AgUiNativeStreamConfig(extra_modes=("custom",))
```

不匹配的流参数会在开始运行前报错。

## 限制同一身份的并发运行

如果同一用户、项目或会话不能同时运行两个任务，可以配置 coordinator。

```python
from tinkerfin import InMemoryRunCoordinator, TinkerFin


coordinator = InMemoryRunCoordinator[str](
    key_resolver=lambda principal: principal,
)
tinkerfin = TinkerFin(run_coordinator=coordinator)

run = tinkerfin.run(source, principal="tenant-7/user-42")
```

有 coordinator 时，每次运行必须提供 `principal`；没有 coordinator 时，`principal` 必须为 `None`。

内存 coordinator 只协调当前进程。如果应用有多个进程，可以实现 `RunCoordinator`，把锁放到共享系统中：

```python
from contextlib import asynccontextmanager


class RedisRunCoordinator:
    @asynccontextmanager
    async def __call__(self, principal: str):
        lock = await acquire_lock(principal)
        try:
            yield
        finally:
            await lock.release()
```

自定义实现需要保证取消时释放锁，并为锁等待设置合理超时。coordinator 只控制并发，不保存 Graph 状态；连续会话仍需要 checkpointer。

## 观察器适合做什么

`on_part` 和 AG-UI 的 `on_event` 适合：

- 写入监控指标；
- 记录审计日志；
- 更新运行进度；
- 在事件交付前执行轻量校验。

观察器会影响主事件流。不要在其中做同步网络请求或无法取消的长任务。

下一篇：[Runtime 使用参考](api-reference.md)。
