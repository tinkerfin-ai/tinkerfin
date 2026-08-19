# AG-UI 入门

[文档首页](../README.md) · [English](../../en/agui/index.md)

AG-UI 把 Agent 的运行过程表示成前端可以理解的事件。前端不必认识 LangGraph 的消息对象，也能显示回答文字、工具调用、状态变化、审批请求和运行结果。

## 什么时候使用

- 你正在做聊天界面，需要边生成边显示回答；
- 你要显示工具正在调用、参数和结果；
- 你要让用户批准或拒绝敏感操作；
- 你要在一个统一协议中展示主 Agent 和子 Agent。

只在服务端处理结果、不需要 AG-UI 前端时，使用[原生 Runtime](../runtime/index.md)更简单。

## 第一个 AG-UI Runtime

```python
import asyncio

from ag_ui.core import RunAgentInput
from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    run_input = RunAgentInput.model_validate(
        {
            "threadId": "conversation-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )

    runtime = agent.new_agui(run_input=run_input)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "你好"}]},
        {"configurable": {"thread_id": "conversation-1"}},
    )

    async for event in events:
        print(event.type)


asyncio.run(main())
```

`run_input` 是前端请求的完整描述；`runtime.astream(...)` 的第一个参数仍然是交给 Graph 的输入。两者用途不同，不要互相替代。

## `RunAgentInput` 字段

| 字段 | 必填 | 作用 |
| --- | --- | --- |
| `threadId` | 是 | 会话 ID；应与 Graph 配置中的 `thread_id` 对应 |
| `runId` | 是 | 本次运行 ID；同一 thread 中每次新运行使用新值 |
| `parentRunId` | 否 | 表示调用方定义的运行父子关系，不表示 LangGraph 子图 |
| `state` | 是 | 前端随请求带来的状态 |
| `messages` | 是 | AG-UI 消息历史 |
| `tools` | 是 | 前端声明的工具描述 |
| `context` | 是 | 前端提供的上下文条目 |
| `forwardedProps` | 是 | 应用自定义的透传属性 |
| `resume` | 否 | 对待处理 interrupt 的恢复决定 |

即使某些列表暂时为空，也要提供完整字段。`threadId` 和 `runId` 不能为空或带首尾空格。

## Runtime 参数

```python
runtime = agent.new_agui(
    run_input=run_input,
    principal=None,
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
| `run_input` | 必填 | 完整 AG-UI 请求 |
| `principal` | `None` | coordinator 使用的业务身份 |
| `on_part` | `None` | 转换前观察每条 LangGraph 数据 |
| `timeout` | `None` | 整条 AG-UI 原生数据流的总时限 |
| `settlement_timeout` | `None` | 取消后等待清理完成的最长秒数 |
| `expose_reasoning_events` | `False` | 是否输出支持的推理事件 |
| `expose_subagent_events` | `True` | 是否把子 Agent 事件发送给前端 |
| `resume` | `None` | 与恢复请求绑定的 `AgUiResumeBinding` |
| `on_event` | `None` | 每条 AG-UI 事件交付前执行的观察函数 |

推理开关不会允许原始 provider 私有数据直接流出。转换过程中仍会清理不应公开的 provider 元数据。

## Runtime 自动使用的流设置

AG-UI 需要同时观察消息、任务和状态，因此默认使用：

```python
stream_mode = ("messages", "tasks", "values")
version = "v2"
subgraphs = True
```

调用 `runtime.astream(...)` 时可以省略这些参数。显式传入时必须满足同样要求；需要额外诊断数据时，可以增加 `updates`、`checkpoints`、`debug` 或 `custom`。

## 下一步

- [看懂 AG-UI 事件](events.md)
- [interrupt 与恢复](interrupts-and-resume.md)
- [只使用 AG-UI 转换器](adapter-extensions.md)
- [AG-UI 使用参考](api-reference.md)
