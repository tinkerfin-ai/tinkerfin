# AG-UI 入门

[文档首页](../README.md) · [English](../../en/agui/index.md)

AG-UI 把 Agent 的文字、工具调用、状态、审批和运行结果表示成前端可消费的事件。框架运行只需要 `Identity`；HTTP 请求是否使用完整 `RunAgentInput`，由你的应用决定。

## 第一个 AG-UI Runtime

```python
import asyncio

from tinkerfin import Identity, TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = Identity(threadId="conversation-1", runId="run-1")
    runtime = agent.new_agui(identity=identity)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "你好"}]},
    )

    async for event in events:
        print(event.type)


asyncio.run(main())
```

这段代码中：

- `Identity.threadId` 会自动成为 Graph checkpoint thread；
- `Identity.runId` 标识这一次语义运行；
- `runtime.astream(...)` 的第一个参数才是交给 Graph 的输入；
- 框架产生的主 `RUN_STARTED.input` 固定为 `None`。

如果前端发送标准 `RunAgentInput`，先在 HTTP 边界校验，再由业务代码创建 `Identity` 和 Graph 输入。各字段的职责不同：

| `RunAgentInput` 字段 | 协议要求 | 应用怎么处理 | Runtime 会自动传给 Graph 吗 |
| --- | --- | --- | --- |
| `threadId` | 必填 | 与 `runId` 一起创建 `Identity`；也是 checkpoint thread | 只自动写入 Graph 配置的 `configurable.thread_id` |
| `runId` | 必填 | 标识本次语义运行；新输入使用新值，重试复用原值 | 否 |
| `parentRunId` | 可选 | 需要运行谱系时由应用保存和解释 | 否 |
| `state` | 必填 | 按应用信任边界校验、保存或转换 | 否 |
| `messages` | 必填 | 保留标准角色、多模态内容和扩展字段，再明确选择本次 Graph 输入 | 否，不会自动注入完整历史 |
| `tools` | 必填 | 作为客户端工具描述保存；不能直接获得服务端执行权限 | 否 |
| `context` | 必填 | 由应用决定是否转换成 Graph context | 否 |
| `forwardedProps` | 必填 | 保存应用扩展参数，例如模型或界面模式 | 否 |
| `resume` | 可选 | 校验待处理 interrupt 后转换成 `Command(resume=...)` | 否 |

有 checkpoint 时尤其不要把完整前端消息历史再次注入 Graph，否则同一条消息可能执行两次。

## `new_agui()` 参数

```python
runtime = agent.new_agui(
    identity=identity,
    mode="default",
    on_part=None,
    timeout=None,
    settlement_timeout=None,
    expose_reasoning_events=False,
    expose_subagent_events=True,
    resume=None,
    on_event=None,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | 本次运行的 thread 和 run 身份 |
| `mode` | Definition 默认值 | `default` 或 `plan`，只控制当前 Runtime 请求 |
| `on_part` | `None` | 转换前观察每条 LangGraph v2 数据 |
| `timeout` | `None` | 整条原生数据流的总时限 |
| `settlement_timeout` | `None` | 调用方等待取消清理的时限；超时不会遗弃清理任务 |
| `expose_reasoning_events` | `False` | 输出支持的公开推理事件 |
| `expose_subagent_events` | `True` | 输出子 Agent 事件 |
| `resume` | `None` | 已验证的 `AgUiResumeBinding` |
| `on_event` | `None` | 每条 AG-UI 事件交付前执行的异步观察函数 |

推理开关不会放行 provider 私有元数据。转换器仍会清理保留字段中的私有推理内容。

## Runtime 固定的流设置

AG-UI 转换需要以下 LangGraph v2 数据：

```python
stream_mode = ("messages", "tasks", "values")
version = "v2"
subgraphs = True
```

通常直接省略。需要额外数据时，可以在 `stream_mode` 中增加 `updates`、`checkpoints`、`debug` 或 `custom`；缺少必需模式、使用 v1 或关闭 subgraphs 会在 Graph 迭代前报错。

## 如果应用需要回显完整请求

框架不知道哪些前端字段可信，也不替业务保存请求。因此它不会自动把 `state`、`messages`、`tools`、`context` 或 `forwardedProps` 写入 `RUN_STARTED.input`。

应用可以在事件发送前增强主开始事件：

```python
if event.type == "RUN_STARTED" and event.run_id == identity.run_id:
    event = event.model_copy(update={"input": canonical_run_input})
```

`canonical_run_input` 应来自应用校验并标准化后的事实，例如服务端重新分配过消息 ID 的请求，而不是未经信任的原始 JSON。

## 下一步

- [看懂 AG-UI 事件](events.md)
- [interrupt 与恢复](interrupts-and-resume.md)
- [只使用转换器](adapter-extensions.md)
- [AG-UI 使用参考](api-reference.md)
