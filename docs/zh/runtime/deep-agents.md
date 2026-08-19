# 创建和运行 Deep Agent

[Runtime 入门](index.md) · [English](../../en/runtime/deep-agents.md)

这一页先讲最常用的 Agent 配置，再补充运行参数。第一次使用不必填写所有参数。

## 常用创建方式

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    name="support-agent",
    model="openai:gpt-5.4",
    tools=[search_orders],
    system_prompt="你负责回答订单问题。",
    checkpointer=MemorySaver(),
)
```

常用参数如下。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `model` | `None` | 模型名称或已经创建的聊天模型；实际使用时通常要提供 |
| `tools` | `None` | Agent 可以调用的工具列表 |
| `system_prompt` | `None` | Agent 的角色、目标和约束 |
| `name` | `None` | Graph 名称，方便日志和调试 |
| `checkpointer` | `None` | 保存 thread 状态；审批恢复和连续会话需要它 |
| `store` | `None` | 保存跨 thread 的长期数据 |
| `debug` | `False` | 是否启用 LangGraph 调试输出 |

## 按需求增加能力

| 如果你需要 | 参数 | 怎么填 |
| --- | --- | --- |
| 使用额外 middleware | `middleware` | 传入 middleware 序列 |
| 调用子 Agent | `subagents` | 传入子 Agent 配置或已编译子 Agent |
| 加载技能目录 | `skills` | 传入目录列表，例如 `['./skills/']` |
| 加载长期记忆文件 | `memory` | 传入文件路径列表 |
| 控制文件操作权限 | `permissions` | 传入 `FilesystemPermission` 列表 |
| 更换文件后端 | `backend` | 传入 Deep Agents backend |
| 在工具执行前请求审批 | `interrupt_on` | 按工具名配置允许的审批动作 |
| 返回结构化结果 | `response_format` | 传入数据模型或结构化输出策略 |
| 扩展运行状态 | `state_schema` | 传入自定义状态类型 |
| 给 middleware 提供上下文 | `context_schema` | 传入上下文类型 |
| 启用 Graph cache | `cache` | 传入 LangGraph cache |

审批必须配合 checkpointer，否则暂停后无法可靠恢复。使用依赖 store 的 backend 或 middleware 时，也要同时提供 `store`。

## 创建一次原生 Runtime

```python
runtime = agent.new(
    principal=None,
    on_part=None,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `principal` | `None` | 并发协调使用的业务身份；没有 coordinator 时必须为 `None` |
| `on_part` | `None` | 每条原生数据返回给调用方之前执行的观察函数 |

## 启动运行

```python
stream = runtime.astream(
    {"messages": [{"role": "user", "content": "检查这个项目"}]},
    {"configurable": {"thread_id": "project-7"}},
)
```

### 输入和配置

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `input` | 必填 | 新消息、状态输入、`Command` 或 `None` |
| `config` | `None` | thread、tags、metadata、递归限制等运行配置 |
| `context` | `None` | 与 `context_schema` 对应的运行上下文 |

### 输出控制

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `stream_mode` | `None` | 选择 `values`、`updates`、`messages`、`tasks` 等输出 |
| `print_mode` | `()` | 额外打印指定模式，不改变返回内容 |
| `output_keys` | `None` | 只返回指定状态字段 |
| `subgraphs` | `False` | 是否包含子图输出 |
| `version` | `"v1"` | LangGraph 输出格式版本；需要统一 envelope 时使用 `"v2"` |
| `debug` | `None` | 覆盖本次运行的调试设置 |

### 中断和持久化控制

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `interrupt_before` | `None` | 在指定节点执行前暂停 |
| `interrupt_after` | `None` | 在指定节点执行后暂停 |
| `durability` | `None` | 控制 checkpoint 的持久化时机 |
| `control` | `None` | 传入 LangGraph 运行控制信息 |
| 其他关键字参数 | 无 | 兼容当前 LangGraph 支持的附加运行参数 |

如果只想读取最终状态，可以使用 `stream_mode="values"`。如果要自己观察完整的 v2 运行数据，通常组合 `messages`、`tasks`、`values`，并启用 `subgraphs=True`。

## 观察每一条数据

`on_part` 必须是异步函数。它在数据交给你的 `async for` 之前执行。

```python
async def record_part(part: object) -> None:
    print("received", part)


runtime = agent.new(on_part=record_part)
```

观察函数抛出的异常会终止运行。不要在其中执行阻塞网络请求；需要写数据库或调用服务时使用异步客户端。

## 常见问题

### 第二次调用 `astream()` 报错

Runtime 是一次性的。重新调用 `agent.new()`。

### 会话没有延续

确认 Agent 配置了 checkpointer，并且后续运行使用相同的 `thread_id`。

### 异步服务启动时卡顿

`agent.new()` 会同步创建 Graph。如果建图较重，在异步服务器中通过受控线程边界调用它。

下一篇：[事件流与 SSE](streams-and-sse.md)。
