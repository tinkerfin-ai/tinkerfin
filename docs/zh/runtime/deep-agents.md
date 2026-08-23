# 创建和运行 Deep Agent

[Runtime 入门](index.md) · [English](../../en/runtime/deep-agents.md)

这一页先讲最常用的 Agent 配置，再补充运行参数。第一次使用不必填写所有参数。

## 常用创建方式

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import Identity, TinkerFin


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

## 执行前审批计划

当请求可能需要补充信息，或需要先确认多步计划时，可以启用 Plan Mode。这个能力属于
TinkerFin，不会改变 Deep Agents 的建图参数。

```python
from tinkerfin import TinkerFin


tinkerfin = TinkerFin(state_schema=AppState)
agent = tinkerfin.plan(
    enabled=True,
    default_mode="default",
    planner_model="openai:gpt-5.4",
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[search_orders],
    checkpointer=production_checkpointer,
)
```

`.plan(...)` 返回新的 TinkerFin 对象，不会修改 `tinkerfin`。新对象继续使用同一个 run
coordinator 和全局 state schema，每个 Definition 固定采用创建它的 factory 能力配置。
`planner_model` 可省略；省略时使用显式配置的 Deep Agent 执行模型。

Plan Mode 按下面的边界处理请求：

1. 一个只读 Planner 判断意图和约束是否充分
2. Planner 只能使用 `ls`、`read_file`、`glob` 和 `grep`
3. 澄清问题可以包含模型动态生成的单选项，并通过 `allow_free_text` 决定是否允许自由输入；需求澄清和
   计划审批通过 LangGraph interrupt 暂停
4. 用户批准后冻结 `ConfirmedPlan`，以原用户消息 ID 提交确定性的 v3 handoff，并立即启动原生 Deep Agent

选择 Plan 时必须提供明确 Planner 模型和具体 `BaseCheckpointSaver`。TinkerFin 不会自动创建
进程内 saver，也不会静默降低 durability；生产环境必须提供生产级 saver。Planning 与原生
Deep Agent 借用同一个 saver、Store、cache、backend 和 runtime context。恢复时必须保持同一个
`Identity.threadId`。Plan 状态位于根状态的 `tinkerfin_plan` 字段，`PlanContent`、
`PlanDraft`、`ConfirmedPlan`、`PlanHandoff`、`PlanState` 等模型从 `tinkerfin.plan` 导入。

`mode="default"` 直接运行原生 Deep Agent Graph，不进入 Planning、不追加 middleware、不替换
state schema，也不创建父 Graph。Todo、Tool/Filesystem HITL、子 Agent、取消和异常语义保持原生行为。

Planner 只拥有用于按需检查 workspace 的只读文件工具；这份受限列表不是执行 Deep Agent 的能力
列表。Planner 可以把用户要求的执行 Tool 写入计划，但不会亲自调用或试运行。Plan 批准后会立即
开始执行，不会再次索要通用 Plan 审批；执行过程中仍保留 `write_file` 等 Tool 自身配置的人工审批。

`.plan(...)` 省略 `clarification_schema` 时使用内置 `DefaultClarificationForm`。需要为问题
或选项增加强类型 metadata 的宿主，可以在应用代码中定义具体 `ClarificationForm`，再把该
类型传给 `.plan(...)`。Schema 固定在返回的 factory 上，不能通过 `new()`、`new_agui()`
或 resume 替换。attributes 必须使用具体 `ClarificationModel` 子类；这些数据会公开给用户，
属于模型生成的规划参考，不能直接作为权限、计费或合规依据。

Python 字段使用 `allow_free_text`，JSON 边界使用 `allowFreeText`。每轮问题数量由绑定的
clarification schema 约束，表单必须非空，用户填写后整组提交。选择 Option 时只提交
`questionId` 和 `optionId`；自由输入时只提交 `questionId` 和 `answer`。工作流从 checkpoint
恢复可信 Form 并派生 Option label，混合、缺失、未知或过期回答都会被拒绝。信息仍不足时，
Planner 会继续下一轮澄清。

完整编辑后的草稿是用户权威约束。Planner 只能继续澄清或接受该草稿，不得静默替换。澄清期间
revision 不变；只有形成完整可审阅草稿时才递增一次。

Plan Mode 固定使用 `sync` checkpoint durability。通常省略 `durability` 即可；显式传入
`sync` 也可以，`async` 和 `exit` 会在事件流开始前报错。

在创建 Runtime 时选择本次请求的 mode：

```python
plan_runtime = agent.new(
    identity=Identity(threadId="project-7", runId="run-1"),
    mode="plan",
)
default_runtime = agent.new_agui(
    identity=Identity(threadId="project-7", runId="run-2"),
    mode="default",
)
```

`default` 使用原生 topology 和 state schema，`plan` 使用独立 Planning Graph。批准后在原生执行前
把有效 mode 切为 `default`。同一 checkpoint thread 的后续请求可以再次选择 Plan；Plan resume
回到 Planning，Tool 与子 Agent resume 直接回到原生 Graph。

## 创建一次原生 Runtime

```python
runtime = agent.new(
    identity=Identity(threadId="project-7", runId="run-1"),
    mode="default",
    on_part=None,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | 本次运行的 `threadId` 与 `runId`，也用于 checkpoint 和并发协调 |
| `mode` | Definition 默认值 | `default` 或 `plan`；普通 Definition 只接受 `default` |
| `on_part` | `None` | 每条原生数据返回给调用方之前执行的观察函数 |

## 启动运行

```python
stream = runtime.astream(
    {"messages": [{"role": "user", "content": "检查这个项目"}]},
)
```

### 输入和配置

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `input` | 必填 | 新消息、状态输入、`Command` 或 `None` |
| `config` | `None` | thread、tags、metadata、递归限制等运行配置 |
| `context` | `None` | 与 `context_schema` 对应的运行上下文 |

一般不用在 `config` 中重复填写 `configurable.thread_id`。如果显式填写，相同值可以使用；与 `Identity.threadId` 不同会在 Graph 迭代、观察器和 coordinator 启动前报错。

### 输出控制

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `stream_mode` | `None` | 选择 `values`、`updates`、`messages`、`tasks` 等输出 |
| `print_mode` | `()` | 额外打印指定模式，不改变返回内容 |
| `output_keys` | `None` | 只返回指定状态字段 |
| `subgraphs` | `False` | 是否包含子图输出 |
| `version` | 自动为 `"v2"` | 原生 Runtime 固定使用 v2；显式传 `"v1"` 会在运行前报错 |
| `debug` | `None` | 覆盖本次运行的调试设置 |

### 中断和持久化控制

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `interrupt_before` | `None` | 在指定节点执行前暂停 |
| `interrupt_after` | `None` | 在指定节点执行后暂停 |
| `durability` | `None` | 控制 checkpoint 持久化时机；Planning 与 handoff 必须使用 `sync` |
| `control` | `None` | 传入 LangGraph 运行控制信息 |
| 其他关键字参数 | 无 | 沿用当前 LangGraph 支持的附加运行参数 |

如果只想读取最终状态，可以使用 `stream_mode="values"`。如果要自己观察完整的 v2 运行数据，通常组合 `messages`、`tasks`、`values`，并启用 `subgraphs=True`。

## 观察每一条数据

`on_part` 必须是异步函数。它在数据交给你的 `async for` 之前执行。

```python
async def record_part(part: object) -> None:
    print("received", part)


runtime = agent.new(identity=identity, on_part=record_part)
```

观察函数抛出的异常会终止运行。不要在其中执行阻塞网络请求；需要写数据库或调用服务时使用异步客户端。

## 常见问题

### 第二次调用 `astream()` 报错

Runtime 是一次性的。重新调用 `agent.new()`。

### 会话没有延续

确认 Agent 配置了 checkpointer，并且后续运行使用相同的 `Identity.threadId`。

### 异步服务启动时卡顿

`agent.new()` 会同步创建 Graph。如果建图较重，在异步服务器中通过受控线程边界调用它。

下一篇：[事件流与 SSE](streams-and-sse.md)。
