# Runtime 入门

[文档首页](../README.md) · [English](../../en/runtime/index.md)

Runtime 负责启动一次 Agent 运行，并把运行过程作为异步数据流交给你。它不会替你创建模型账号，也不会接管数据库、checkpointer、store 或 Sandbox。

## 什么时候使用

| 你的目标 | 使用方式 |
| --- | --- |
| 直接处理 LangGraph 原生数据 | `agent.new()` |
| 给前端发送 AG-UI 事件 | `agent.new_agui()` |
| 运行自己的异步事件源 | `TinkerFin.run()` |

第一次使用时，先从 `agent.new()` 开始。

## 安装

```bash
pip install tinkerfin
```

模型供应商的依赖和密钥需要按模型自己的要求安装、配置。

## 第一个 Runtime

```python
import asyncio

from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new()
    stream = runtime.astream(
        {"messages": [{"role": "user", "content": "用一句话介绍北京"}]},
        {"configurable": {"thread_id": "conversation-1"}},
    )

    async for part in stream:
        print(part)


asyncio.run(main())
```

这段代码分为四步：

1. `TinkerFin()` 创建入口对象。
2. `create_deep_agent(...)` 保存 Agent 的模型、工具和其他配置。
3. `agent.new()` 创建一个新的 Graph 和一次性 Runtime。
4. `runtime.astream(...)` 启动运行，并逐条返回结果。

## `thread_id` 有什么用

`thread_id` 表示一段会话。如果配置了 checkpointer，相同的 `thread_id` 可以继续之前的状态。

```python
config = {"configurable": {"thread_id": "user-42-support"}}
```

不要为同一段连续会话随机更换 `thread_id`。不同用户也不要共用同一个值。

## 一次性使用

每个 Runtime 只能调用一次 `astream()`。需要再次运行时，重新调用 `agent.new()`：

```python
first = agent.new()
second = agent.new()
```

Definition 可以重复使用；Runtime 只代表一次运行。

## 下一步

- [创建和运行 Deep Agent](deep-agents.md)
- [事件流与 SSE](streams-and-sse.md)
- [自定义事件源与并发协调](extensions.md)
- [Runtime 使用参考](api-reference.md)

