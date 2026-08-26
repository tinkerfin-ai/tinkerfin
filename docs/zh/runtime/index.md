# Runtime 入门

[文档首页](../README.md) · [English](../../en/runtime/index.md)

Runtime 负责启动一次 Agent 运行，并把运行过程作为异步数据流交给你。它不会替你创建模型账号，也不会接管数据库、checkpointer、store 或 Sandbox。

## 什么时候使用

| 你的目标 | 使用方式 |
| --- | --- |
| 直接处理 LangGraph 原生数据 | `agent.new()` |
| 给前端发送 AG-UI 事件 | `agent.new_agui()` |

第一次使用时，先从 `agent.new()` 开始。

## 安装

```bash
pip install tinkerfin
```

模型供应商的依赖和密钥需要按模型自己的要求安装、配置。

## 第一个 Runtime

```python
import asyncio

from tinkerfin import Identity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = Identity(threadId="conversation-1", runId="run-1")
    runtime = agent.new(identity=identity)
    stream = runtime.astream(
        {"messages": [{"role": "user", "content": "用一句话介绍北京"}]},
    )

    async for part in stream:
        print(part)


asyncio.run(main())
```

这段代码分为四步：

1. `TinkerFin()` 创建入口对象。
2. `create_deep_agent(...)` 保存 Agent 的模型、工具和其他配置。
3. `agent.new(identity=...)` 创建新的 Graph，并绑定本次运行身份。
4. `runtime.astream(...)` 启动运行，并逐条返回结果。

## `Identity` 有什么用

`Identity` 只包含 `threadId` 和 `runId`。Runtime 会把 `threadId` 自动写入 Graph 配置，因此调用时不用再重复填写。

```python
identity = Identity(threadId="user-42-support", runId="run-20260820-1")
```

| 字段 | 要求 | 作用 |
| --- | --- | --- |
| `threadId` | 必填、非空、不能有首尾空白 | 一段可继续的会话，也是 Graph checkpoint thread |
| `runId` | 必填、非空、不能有首尾空白 | thread 中一次语义运行的幂等 ID |

`Identity` 创建后不可修改，也不接受额外字段。parent 谱系、用户身份和请求正文都不属于它。
只有需要谱系时才向 `new_agui(parent_run_id=...)` 传入。公开事件、Graph、checkpoint、协调和
持久投递共同使用这一个 canonical Identity。

同一段连续会话复用 `threadId`，每次新的语义运行使用新的 `runId`。网络重试或重新附着同一次运行时复用原来的 `runId`。

## 一次性使用

每个 Runtime 只能调用一次 `astream()`。需要再次运行时，重新调用 `agent.new()`：

```python
first = agent.new(identity=Identity(threadId="thread-1", runId="run-1"))
second = agent.new(identity=Identity(threadId="thread-1", runId="run-2"))
```

Definition 可以重复使用；Runtime 只代表一次运行。

## 下一步

- [创建和运行 Deep Agent](deep-agents.md)
- [事件流与 SSE](streams-and-sse.md)
- [运行协调与 Redis 租约](extensions.md)
- [Runtime 使用参考](api-reference.md)
