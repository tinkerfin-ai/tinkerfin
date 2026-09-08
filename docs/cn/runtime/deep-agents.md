# 创建和运行 Deep Agent

[Runtime 入门](index.md) · [English](../../en/runtime/deep-agents.md)

这一页先讲最常用的 Agent 配置，再补充运行参数。第一次使用不必填写所有参数。

## 常用创建方式

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import RunIdentity, TinkerFin


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
3. 每道澄清问题由 Planner 选择 `single_choice`、`multiple_choice`、`text`、`date`、`time` 或 `datetime`，需求澄清和计划审批通过 LangGraph interrupt 暂停
4. 用户批准后冻结 `ConfirmedPlan`，以原用户消息 ID 提交确定性 handoff，并立即启动原生 Deep Agent

Plan 审阅默认允许 `PlanReviewAction.APPROVE`、`RESPOND` 和 `REJECT`。如需改变允许动作，
通过 `allowed_review_actions` 传入非空且不重复的有序集合；响应 Schema 只包含实际配置的动作。只有宿主
提供可信计划编辑器时才应显式启用 `EDIT`。

只有批准会退出 Plan 模式并开始原生执行。拒绝会使当前草稿失效，接受可选原因，生成一次用户可见的
Planner 回复，然后等待下一条 Plan 输入。显式配置的 `CANCEL` 采用相同续接但不携带原因。AG-UI
传输层取消仍表示放弃，不会调用 Planner。

选择 Plan 时必须提供具体 `BaseCheckpointSaver` 和一个显式模型。Agent Definition 已提供
`model` 时可以省略 `planner_model`；两者都省略会被拒绝。TinkerFin 不会自动创建进程内 saver，
也不会静默降低 durability；生产环境必须提供生产级 saver。Planning 与原生
Deep Agent 借用同一个 saver、Store、cache、backend 和 runtime context。恢复时必须保持同一个
`RunIdentity.threadId`。Plan 状态位于根状态的 `tinkerfin_plan` 字段，`PlanContentModel`、
内置结构化与 Markdown 内容类型、`PlanDraft`、`ConfirmedPlan`、`PlanHandoff`、`PlanState`
等模型从 `tinkerfin.plan` 导入。

计划内容默认使用 `StructuredPlanContent`。需要一份原样 Markdown 或宿主自定义内容契约时，
在 Plan factory 上选择：

```python
from tinkerfin.plan import MarkdownPlanContent


agent = tinkerfin.plan(
    planner_model="openai:gpt-5.4",
    content_schema=MarkdownPlanContent,
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[search_orders],
    checkpointer=production_checkpointer,
)
```

自定义内容类型继承 `PlanContentModel`，并使用精确的 Pydantic 字段。运行时自动为 Definition
固定并计算所选 Schema 的 fingerprint；草稿审阅、显式启用的编辑、确认、checkpoint 恢复和
执行 handoff 始终使用同一份已校验内容。

`mode="default"` 直接运行原生 Deep Agent Graph，不进入 Planning、不追加 middleware、不替换
state schema，也不创建父 Graph。Todo、Tool/Filesystem HITL、子 Agent、取消和异常语义保持原生行为。

Planner 只拥有用于按需检查 workspace 的只读文件工具；这份受限列表不是执行 Deep Agent 的能力
列表。Planner 可以把用户要求的执行 Tool 写入计划，但不会亲自调用或试运行。Plan 批准后会立即
开始执行，不会再次索要通用 Plan 审批；执行过程中仍保留 `write_file` 等 Tool 自身配置的人工审批。

`.plan(...)` 省略 `clarification_schema` 时使用内置 `DefaultClarificationForm`。
`BuiltInClarificationForm[QuestionAttributes, OptionModel]` 可以一次为六类内置题型增加共享强类型
metadata，不需要分别创建题型子类。宿主也可以提供只包含客户端已支持题型的具体
`ClarificationForm` 联合。attributes 会公开给用户，属于模型生成的规划参考，不能直接作为权限、
计费或合规依据。

Choice 题在 Python 使用 `allow_free_text`，JSON 使用 `allowFreeText`。多选题还声明
`min_selections` 和可选 `max_selections`，已选 Option ID 可以和一个自定义答案共存。文本答案必须
非空；日期固定使用不含时间和时区的 `YYYY-MM-DD`。时间答案表示题目所声明 IANA 时区中的本地分钟，
可选的 `minimum` 和 `maximum` 是包含边界且不得跨越午夜。完整回答以 checkpoint question ID 为 key，
每个 value 使用 `status: answered` 和对应 `answerType`，可选题跳过时使用 `status: skipped`。
Planning 在 Graph resume 前校验 pending 的精确响应 Schema，从可信 Form 派生 Option label、规范化
答案顺序，并把明确跳过保留为 Planner 上下文。
日期时间题要求 IANA 时区并接收一个本地日期与分钟，范围可以跨日期；规范化会增加唯一 UTC
`instant`，DST 不存在时间或重复的本地分钟会被拒绝。


完全自定义语义题型通过 `clarification_type(...)` 注册。一个冻结描述符同时提供无版本 namespaced
ID、模型选择说明、Question/Response Model、可选的逐题 Schema 与校验器，以及 canonical JSON
normalizer。所有 callback 必须同步、确定性且不执行外部 I/O；宿主客户端必须为 Form 中的每个自定义
题型提供 renderer。Definition fingerprint 固定唯一当前 Schema 与 callback；合同变化时重建不兼容
存量数据，不得引入另一个 type ID 版本。

配置 `PlanReviewAction.EDIT` 后，完整编辑的草稿是用户权威约束。Planner 只能继续澄清或接受
该草稿，不得静默替换。澄清期间 revision 不变；只有形成完整可审阅草稿时才递增一次。

Plan Mode 固定使用 `sync` checkpoint durability。通常省略 `durability` 即可；显式传入
`sync` 也可以，`async` 和 `exit` 会在事件流开始前报错。

每次 managed run 都可以选择 mode：

```python
plan_stream = await tinkerfin.open_run(
    RunIdentity(threadId="project-7", runId="run-1"),
    agent=agent,
    input=graph_input,
    mode="plan",
)
default_events = await tinkerfin.open_agui_run(
    RunIdentity(threadId="project-7", runId="run-2"),
    agent=agent,
    input=graph_input,
    mode="default",
)
```

`default` 使用原生 topology；`plan` 使用独立 Planning Graph。批准后在原生执行前把有效 mode
切为 `default`。同一 checkpoint thread 后续可以再次选择 Plan；Plan resume 回到 Planning，
Tool 与子 Agent resume 回到原生 Graph。

## 打开一次 managed 原生运行

```python
stream = await tinkerfin.open_run(
    RunIdentity(threadId="project-7", runId="run-1"),
    agent=agent,
    input={"messages": [{"role": "user", "content": "检查这个项目"}]},
    mode="default",
    config=config,
    context=context,
    on_native_part=record_part,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | checkpoint 与协调共用的 thread 和 run 身份 |
| `agent` | 必填 | Definition，或返回 Definition 的同步/异步 callable |
| `input` | 必填 | 新 state、原生 `Command` 或 `None` |
| `mode` | Definition 默认值 | `default` 或 `plan`；普通 Definition 只接受 `default` |
| `config` | `None` | tags、metadata、递归限制等 Graph 配置 |
| `context` | `None` | 与 `context_schema` 对应的 Runtime context |
| `on_native_part` | `None` | 每条原生数据交给消费方前执行的异步观察函数 |
| 其他关键字参数 | 无 | 当前 Profile 支持的 Graph stream 参数 |

一般不在 `config` 中重复填写 `configurable.thread_id`。显式相同值可以使用，不同值会在 Graph
迭代、Observation 与协调启动前报错。所选 Profile 负责必需 stream mode、version、subgraph
范围与完整 state 输出。

`on_native_part` 会在交付路径中等待。不要在其中执行阻塞网络或数据库访问，应使用异步客户端。

## 只返回 managed 最终 state

调用方不需要逐条消费 stream part 时，继续使用同一个门面：

```python
state = await tinkerfin.ainvoke(
    RunIdentity(threadId="project-7", runId="run-1"),
    agent=agent,
    input={"messages": [{"role": "user", "content": "检查这个项目"}]},
)
```

`ainvoke()` 会在框架内部消费 Profile 提供的 canonical stream，并保留 Runtime Observation、
Trace、身份绑定、取消、终态结算与资源清理，最后返回根 state 的防御性副本。只有下方明确的
unmanaged 高级场景才使用 `await agent.create_graph().ainvoke(...)`。

## 复用 Direct Graph

不需要 managed run 生命周期的高级集成可以创建一个异步 Runnable，并跨 thread ID 复用：

```python
graph = await agent.create_graph(mode="plan")
state = await graph.ainvoke(graph_input, config=config)
async for part in graph.astream(graph_input, config=config):
    consume(part)
```

Direct Graph 负责 Plan/native 路由和基于 checkpoint 的 resume 选择，但不会创建 `RunIdentity`、
Runtime Observation、Trace、AG-UI、Messaging 或宿主业务状态。Direct resume 使用 LangGraph
`Command(resume=...)`。

## 常见问题

### 同一个 managed stream 第二次迭代报错

Managed stream 只能使用一次。下一个运行使用新的 run identity 再调用 `open_run()`。

### 会话没有延续

确认 Agent 配置了 checkpointer，并且后续运行复用同一个 `RunIdentity.threadId`。

### Graph 构造阻塞事件循环

内置 Profile 会通过 AnyIO 有容量限制的 worker 边界调用锁定的同步 Deep Agents factory。
不要在应用层为 `open_run()`、`open_agui_run()` 或 `create_graph()` 再套一层构造线程。

自定义 Profile 的 factory 原生异步时，可以实现 `create_agent_graph(factory, args, kwargs)`。
未实现这个可选方法的 Profile 继续拥有自己的同步 factory，由 Core 通过同一个有容量限制的 worker
边界调用。

下一篇：[事件流与 SSE](streams-and-sse.md)。
