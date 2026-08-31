# Runtime 入门

[文档首页](../README.md) · [English](../../en/runtime/index.md)

TinkerFin 打开一次 managed Agent run，并把运行过程作为异步流交给调用方。模型、数据库、
checkpointer、Store 与 Sandbox 资源仍由宿主管理。

## 选择入口

| 目标 | 使用方式 |
| --- | --- |
| 处理已校验的 LangGraph 原生数据 | `TinkerFin.open_run()` |
| 给前端发送 AG-UI 事件 | `TinkerFin.open_agui_run()` |
| 在 managed 生命周期外复用异步 Runnable | `DeepAgentDefinition.create_graph()` |

普通项目从 `open_run()` 开始。Direct Graph 属于高级边界，不会自动创建 run identity、Runtime
Observation、Trace、AG-UI、Messaging 或业务生命周期。

## 安装

```bash
pip install tinkerfin
```

基础安装包含原生 Runtime、Plan Mode、Observation 与原生 SSE。使用 `open_agui_run()` 前安装
`pip install "tinkerfin[agui]"`。

模型供应商依赖和密钥仍按供应商要求配置。下方 OpenAI 模型字符串需要
`pip install langchain-openai`。

## 第一次 managed run

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    stream = await tinkerfin.open_run(
        RunIdentity(threadId="conversation-1", runId="run-1"),
        agent=agent,
        input={"messages": [{"role": "user", "content": "用一句话介绍北京"}]},
    )
    async for part in stream:
        print(part)


asyncio.run(main())
```

普通路径只有三步：

1. 配置可复用的 `TinkerFin` 门面
2. 创建可复用的 Agent Definition
3. 通过 `open_run()` 传入身份和输入，并消费返回的流

门面会按需解析惰性 Agent callback，通过所选 Profile 的原生异步方法或有容量限制的同步 factory
边界构造 Graph，注入身份，打开 Observation 和协调作用域，并负责取消与清理。

## `RunIdentity` 的作用

`RunIdentity` 只包含 `threadId` 和 `runId`。TinkerFin 会自动把 `threadId` 注入 Graph 配置，
调用方不应重复填写。

```python
identity = RunIdentity(threadId="user-42-support", runId="run-20260820-1")
```

| 字段 | 要求 | 作用 |
| --- | --- | --- |
| `threadId` | 必填、非空、不能有首尾空白 | 连续会话和 checkpoint thread |
| `runId` | 必填、非空、不能有首尾空白 | 一次语义运行的幂等 ID |

该值不可修改，每个 ID 最多 1,024 个字符，并拒绝额外字段。parent 谱系、认证与请求正文属于
独立输入。同一段连续会话复用 `threadId`；新的语义输入使用新的 `runId`，只有同一次运行的
重试或附着才复用它。

## Managed stream 与 Direct Graph

`open_run()` 和 `open_agui_run()` 返回的流都只能消费一次，并支持显式关闭。Agent Definition
可以复用，也可以同时服务不同 thread ID。

高级集成可以直接创建可复用异步 Runnable：

```python
graph = await agent.create_graph(mode="plan")
result = await graph.ainvoke(graph_input, config=config)
```

`DeepAgentGraph` 支持 `ainvoke()`、`astream()`、`abatch()` 与标准 Runnable 组合。Direct resume
使用原生 LangGraph `Command(resume=...)`。同步 `invoke()`、`stream()` 和 `batch()` 会明确拒绝，
从而保持异步 checkpointer、Store、Tool 与取消语义。

## 当前能力边界

框架发行提供显式 `deepagents-v2` Runtime Profile，不提供 Deep Agents v3 Profile 或通用
TodoGroups Projection/UI。Studio 在查询时从 canonical Trace 事实派生产品任务轨迹。其他具体
Profile 必须拥有对应上游构造与流合同，同时输出同一 canonical Native Observation；下游
Runtime、Trace、AG-UI 与 Messaging 不按上游版本分支。

当前不提供 Archive/S3/Blob、payload Encryption/KMS 或 OpenTelemetry exporter。具体集成只能
通过 `TraceLedgerBackend` 接入活动 Trace 存储；高级集成可完整替换 `TraceStore`、包装 canonical
payload codec、观察 `RuntimeObserver` 或装饰 Store/Messaging Backend 边界。项目不提供空占位接口。

## 下一步

- [创建和运行 Deep Agent](deep-agents.md)
- [事件流与 SSE](streams-and-sse.md)
- [运行协调与 Redis 租约](extensions.md)
- [记录语义执行历史](../tracing/index.md)
- [Runtime 使用参考](api-reference.md)
