# 配置智能体

[Runtime](index.md) · [English](../../en/runtime/deep-agents.md)

`build()` 接收 Runtime 使用的 Deep Agents 能力，并返回可复用的 `AgentRuntime`。

```python
from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("support")
    .build(
        name="support-agent",
        model="openai:gpt-5.4",
        tools=[search_orders],
        system_prompt="回答客户订单相关问题。",
        store=store,
    )
)
```

## 构建选项

| 能力 | 参数 |
| --- | --- |
| 模型与指令 | `model`、`system_prompt` |
| 工具与 middleware | `tools`、`middleware` |
| 子智能体 | `subagents` |
| 技能与记忆文件 | `skills`、`memory` |
| 文件系统 | `backend`、`permissions` |
| 每次运行动态工具 | `prepare_tools` |
| 工具审批 | `interrupt_on` |
| 结构化输出 | `response_format` |
| 状态与调用上下文 | `state_schema`、`context_schema` |
| 持久化与缓存 | `checkpointer`、`store`、`cache` |

传入的资源仍由应用管理。`build()` 只校验和保存配置，不执行 I/O。

使用 Store 的文件、记忆、技能和摘要 middleware 时，不要为 `StoreBackend` 单独传入
`store`，统一通过 `build(store=...)` 提供。Runtime 为这些内置声明应用 namespace
隔离和异步文件访问，不修改调用方提供的配置对象。

## 持久化与审批

可恢复的工具审批和 Plan 必须使用具体 checkpointer。checkpointer 已保存 thread 历史时，只提交新的用户消息。

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .build(
        model=model,
        tools=[send_report],
        interrupt_on={"send_report": True},
    )
)
```

审批流程使用 `durability="sync"`，表示等待异步 checkpoint 写入完成，不表示使用同步数据库驱动。

## 执行前审阅 Plan

在 builder 上启用 Plan，并在请求中选择 `mode="plan"`：

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .with_plan(planner_model=model)
    .build(model=model, tools=tools)
)

result = await runtime.ainvoke(
    thread_id=thread_id,
    run_id=run_id,
    input=graph_input,
    mode="plan",
)
```

Plan 可以澄清需求，并在执行前请求用户审阅计划。其内置文件访问只读。
`mode="default"` 直接执行智能体。Plan 数据模型和审阅动作由 `tinkerfin.plan` 导出。

## 惰性 workspace 与工具

一次运行需要 Sandbox 或其他待准备文件系统时，把 `Workspace` 声明传给 backend：

```python
runtime = (
    TinkerFin()
    .with_namespace(namespace)
    .build(
        model=model,
        backend=sandboxes.workspace(workspace_key),
    )
)
```

应用自行决定 `workspace_key`，可以按用户、session 或项目划分。Runtime 将自己的 namespace 与该 key 组合，只为已准入的运行准备 workspace，并在清理时释放本次运行的句柄。

`prepare_tools` 可根据本次运行身份和已准备的 workspace 创建额外异步工具。每次运行只调用一次。内置文件、Todo 和 task 工具名是保留名称。

## 子智能体

可直接使用 Deep Agents 的 compiled 或 remote subagent，也可用
`tinkerfin.subagents.SubAgent` 声明带类型的子智能体。声明式子智能体可以配置自己的工具和 workspace 准备。注册的 compiled subagent 继承 Runtime 的 checkpointer 和 Store，不应另行固定 checkpoint 坐标。

## 直接使用 Graph

高级调用方可以构建异步 Graph：

```python
from tinkerfin.deep_agent import create_graph

graph = await create_graph(runtime)
result = await graph.ainvoke(graph_input, config=config)
```

直接 Graph 的执行和清理由调用方负责。它不会创建受管 Runtime 观察、AG-UI 事件、Messaging 投递或每次运行的 workspace 准备。配置了 `workspace(...)` 或 `prepare_tools` 的 Runtime 必须使用受管执行入口。

下一步：[流与 SSE](streams-and-sse.md)。
