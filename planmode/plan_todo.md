# TinkerFin Plan 能力：稳定父工作流与请求级 mode

## Summary

Plan 能力使用不可变的 TinkerFin factory 配置，不占用 Deep Agents factory 参数；每次
Runtime 请求独立选择 mode：

```python
agent = tinkerfin.plan(
    enabled=True,
    default_mode="default",
    gate_model="openai:gpt-5.4-mini",
    planner_model="openai:gpt-5.4",
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
    checkpointer=checkpointer,
    store=store,
)

runtime = agent.new(
    identity=identity,
    mode="plan",  # 或 "default"
)
```

`TinkerFin().create_deep_agent(...)` 使用普通 Deep Agent topology；`.plan(...)` 返回对象
创建的 Definition 始终使用同一张稳定外层 LangGraph：

```text
START → mode
          ├─ default ──────────────────────────────────────┐
          └─ plan → Gate                                   │
                     ├─ direct ────────────────────────────┤
                     ├─ clarify → Gate Clarification → Gate│
                     └─ plan → Read-only Planner           │
                                  ├─ clarify → Planner Clarification → Planner
                                  └─ draft → Plan Review
                                               ├─ edit → Plan Review
                                               ├─ respond → Planner
                                               ├─ reject → CANCELLED
                                               └─ approve → ConfirmedPlan
                                                                    ↓
                                                           DeepAgent 子图
                                                                    ↓
                                                               COMPLETED
```

- 父图唯一持有具体 `checkpointer`、`store` 和 `cache`
- Gate、Planner、DeepAgent 均以子图方式编译，使用 `checkpointer=None`、`store=None`、`cache=None` 继承父运行时
- Plan Mode 强制使用 `durability="sync"`，保证审批状态先提交再发布 interrupt terminal
- 第一阶段不拆 Stage；确认后由一个 DeepAgent 完成整项任务
- Plan 实现位于独立 `tinkerfin.plan` 子包；Runtime 与原 Deep Agent façade 只负责不可变能力、mode 绑定和 factory 分派

## Public contracts and state

- `TinkerFin(state_schema=...)` 为该 factory 创建的全部 Deep Agent Definition 提供全局 state schema
- `TinkerFin.plan(...)` 返回新 TinkerFin 对象并保留同一个 coordinator 与全局 state schema；已创建 Definition 的拓扑不受后续配置影响
- `create_deep_agent(...)` 的运行时签名与 `.pyi` 参数继续严格等于锁定版本 Deep Agents
- Plan-capable Definition 必须传入明确 model 和具体 `BaseCheckpointSaver`；`None`、`True`、`False` saver 均提前报 `PlanModeConfigurationError`
- `DeepAgentDefinition.new/new_agui(mode=...)` 冻结本次请求的 `default | plan`；省略时使用 `.plan(default_mode=...)`
- 普通 Definition 只接受 `default`；Plan-capable Definition 在同一 topology、state schema 和 checkpoint thread 上支持逐轮切换
- mode 是框架保留的请求控制数据，不进入调用方 state，调用方不能通过 configurable 覆盖
- `store` 仍可选：Plan 状态存入 checkpointer；Store 只承担跨 thread 长期记忆
- 导出严格、冻结的 Pydantic 契约：
  - `PlanStep(id, title, description, verification)`
  - `PlanDraft(schema_version=1, revision, goal, assumptions, steps, acceptance_criteria)`
  - `ConfirmedPlan`：批准版本的不可变快照
  - `PlanStatus`：`planning / awaiting_clarification / awaiting_review / executing / completed / cancelled`
- 父状态自动组合全局 state、Definition state、middleware 公开 state、files、todos 和 Plan state，保留原有 `Annotated` reducer、Required/NotRequired 与自定义字段；同名合同冲突时提前失败
- context 继续使用 Deep Agents `context_schema`，不与 state 合并，也不写入 checkpoint
- checkpoint 中的 Plan、Gate 和 Planner 结构化结果使用纯 JSON；Pydantic 模型和 enum 仅在节点边界校验
- 新普通输入从 START 重置上一次 Plan 的瞬态字段；`Command(resume=...)` 从 checkpoint 继续，不重复初始化
- `ConfirmedPlan` 是用户契约；DeepAgent 的 `todos` 只是执行遥测，不反向改写计划步骤，避免两套计划状态互相竞争

## Workflow and AG-UI changes

- Gate 使用同一模型的结构化输出，采取保守策略：
  - 仅“输入充分、单步、低风险”才能 `direct`
  - 明确要求规划、复杂任务或不确定时进入 `plan`
  - 缺少会实质改变结果的信息时进入 `clarify`
- Planner 是独立的轻量 `create_agent` 子图：
  - 复用执行 Agent 的模型、backend、context
  - 只暴露 `ls/read_file/glob/grep`
  - 不暴露写工具、`execute` 或调用方自定义工具
  - 输出 `PlanDraft` 或结构化澄清问题；问题可动态生成单选项，并决定是否允许自由输入
- DeepAgent 保留调用方原有 tools、middleware、backend、subagents 与 `interrupt_on`
  - 新增内部执行 middleware，从 state 读取 `ConfirmedPlan` 并追加到 system message
  - 不向对话消息写入隐藏的计划控制文本
  - direct 路径不注入计划
- 在 `tinkerfin-agui-adapter` 增加通用、版本化的 `RuntimeInterruptEnvelope`，支持 `plan_clarification` 和 `plan_review`
  - Plan state 通过根 `STATE_SNAPSHOT` 发布
  - interrupt 携带 `message`、`responseSchema`、revision 和可信 metadata
  - 保持 `STATE_SNAPSHOT → MESSAGES_SNAPSHOT → RUN_FINISHED(interrupt)` 顺序
- Plan Review payload 固定为：
  - `approve + baseRevision`
  - `edit + baseRevision + 完整 draft`
  - `respond + baseRevision + message`
  - `reject + baseRevision + 可选 message`
- `ResumeMapper` 校验完整覆盖、可信 runtime envelope 和 JSON object payload，再生成顶层 `Command(resume=...)`；父 Graph 校验 Plan payload 并拒绝过期 revision
  - Plan interrupt 的 `prior_tool_call_ids` 为空
  - DeepAgent tool review 继续使用现有 decisions 映射
  - `status=cancelled` 仍表示 abandon，绝不伪造 reject
  - MVP 明确拒绝同一批次混合 Plan 与 Tool interrupt
- `tinkerfin-messaging` 不改协议：现有事件持久化和游标重连已经覆盖浏览器 SSE 断流；进程 owner 丢失后的 Graph 自动重建留到后续阶段

## Test plan

- API：普通 TinkerFin 行为与上游签名保持不变；开启时校验 model、saver、链式类型和稳定工作流版本
- 所有权 contract test：父图持有 saver/store，三个子图保持 `None`；同一 `thread_id` 下产生独立 checkpoint namespace
- Gate：direct、clarify、plan、显式规划请求及不确定场景
- Planner：只读工具集合、写工具/自定义工具不可见、结构化输出、缺失输入、非法/重复步骤 ID
- Review：approve、完整 edit、自然语言 respond、reject、过期 revision、重复 resume、未知 interrupt 和 cancellation
- 端到端：计划确认后执行、direct 执行、澄清后规划、编辑后重新确认
- mode：同一 thread 的 `plan → default → plan`、default 绕过 Gate、mode 不改变 topology/state/checkpointer/store
- Studio：thread 级 mode、历史恢复、Plan abandon、Tool HITL 独立、active stream 禁用切换
- DeepAgent 嵌套生命周期：tool interrupt 传播到根、顶层 Command 恢复、store 继承、Tool ID 不变、唯一 terminal
- 固化真实 v2 `messages/tasks/values + subgraphs=True` 序列断言，覆盖根/子 namespace、快照顺序、正常结束、异常和取消
- 先运行定向测试，再运行 `ruff format --check`、`ruff check`、Pyright 和仓库全量 pytest；同步英文包文档与中英文项目文档

## Deferred scope and evidence

- 第二阶段：StageRun、Stage Guard、重试预算、局部返工、Final Guard、自动重规划
- 第三阶段：执行中 change request、安全暂停、workflow-version registry、进程重启恢复和 recoverable producer
- 已验证版本：Deep Agents 0.7.5、LangGraph 1.2.10、LangChain 1.3.14、AG-UI 0.1.19；本地源码与官方 tag 哈希一致
- 官方依据：
  - [LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
  - [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
  - [LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)
  - [LangGraph 1.2.10 Checkpointer source](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py)
  - [Deep Agents 0.7.5 graph source](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.5/libs/deepagents/deepagents/graph.py)
  - [OpenAI Codex Plan Mode guidance](https://developers.openai.com/codex/learn/best-practices)
  - [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes)
