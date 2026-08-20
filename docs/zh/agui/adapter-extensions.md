# 只使用 AG-UI 转换器

[interrupt 与恢复](interrupts-and-resume.md) · [English](../../en/agui/adapter-extensions.md)

如果应用已经自己创建和运行 LangGraph，只需要把原生 v2 数据转换成 AG-UI，可以单独使用 `tinkerfin-agui-adapter`。

## 安装

```bash
pip install tinkerfin-agui-adapter
```

转换器不创建 Graph、不调用模型、不读取 checkpoint，也不提供 HTTP 服务。

## 最简单的转换方式

```python
from tinkerfin_agui_adapter import Identity, astream_events


parts = graph.astream(
    graph_input,
    config,
    stream_mode=("messages", "tasks", "values"),
    version="v2",
    subgraphs=True,
)

events = astream_events(
    parts,
    identity=Identity(threadId="thread-1", runId="run-1"),
)

async for event in events:
    await send_event(event)
```

`astream_events()` 会生成 `RUN_STARTED`、转换中间事件，并保证一个主终止事件。它会独占并关闭传入的异步迭代器，因此同一份 `parts` 不能再交给另一个消费者。

### 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `parts` | 必填 | LangGraph v2 异步数据流 |
| `identity` | 必填 | 本次转换产生的主 thread 和 run 身份 |
| `expose_reasoning_events` | `False` | 是否产生支持的推理事件 |
| `expose_subagent_events` | `True` | 是否交付子 Agent 事件 |
| `prior_tool_call_ids` | `frozenset()` | 恢复前已经完整发送过的 scoped Tool ID |

## 编码成 SSE

```python
from tinkerfin_agui_adapter import encode_sse


async for event in events:
    frame = encode_sse(event, event_id=next_event_id())
    await send_text(frame)
```

`event_id` 可以是字符串或整数。`encode_sse()` 只编码一条事件，不负责保存、重试、回放或取消。

## 如果你要自己控制主生命周期

只有自定义编排器才需要逐条使用 `DeepAgentAgUiAdapter`：

```python
from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
)


lifecycle = AgUiLifecycleEventFactory()
identity = Identity(threadId="thread-1", runId="run-1")
adapter = DeepAgentAgUiAdapter(identity=identity)

await send_event(lifecycle.started(identity=identity))
try:
    async for part in parts:
        for event in adapter.process(part):
            await send_event(event)
    for event in adapter.finish():
        await send_event(event)
    await send_event(
        lifecycle.finished(
            identity=identity,
            outcome=adapter.main_outcome(),
        )
    )
except Exception:
    for event in adapter.abort(code="runtime_error"):
        await send_event(event)
    await send_event(
        lifecycle.failed(
            identity=identity,
            message="Agent run failed",
            code="runtime_error",
        )
    )
```

这里由你的代码负责“一个开始、一个终止”。一般应用优先使用 `astream_events()`，因为它已经处理了关闭、取消和失败顺序。

## 如果事件太碎

`micro_batch()` 可以合并连续的文字和工具参数增量，减少前端收到的事件数量，同时保留生命周期边界：

```python
from tinkerfin_agui_adapter import micro_batch


batched = micro_batch(events)
async for event in batched:
    await send_event(event)
```

## 如果需要保存自己的 ID

`ScopedIdCodec` 把 namespace、对象类型和原始 ID 编成完整 ID：

```python
from tinkerfin_agui_adapter import ScopedIdCodec


codec = ScopedIdCodec()
public_id = codec.encode("tool", ("researcher",), "call-7")
kind, namespace, raw_id = codec.decode(public_id)
```

不要截断 scoped ID，也不要只保存原始 Tool ID；不同 namespace 中可能出现相同原始 ID。

下一篇：[AG-UI 使用参考](api-reference.md)。
