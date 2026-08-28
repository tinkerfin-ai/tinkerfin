# AG-UI 使用参考

[AG-UI 入门](index.md) · [English](../../en/agui/api-reference.md)

## 高层 Runtime 能力

| API | 用途 |
| --- | --- |
| `DeepAgentDefinition.new_agui(...)` | 创建一次 AG-UI Runtime |
| `DeepAgentDefinition.prepare_agui_resume(...)` | 从权威 Graph checkpoint 解析客户端决定 |
| `DeepAgentAgUiRuntime.astream(graph_input, ...)` | 执行普通 Graph 请求 |
| `DeepAgentAgUiResumeRuntime.astream(...)` | 执行已绑定恢复，不接收调用方 input |
| `AgUiEventStream` | 迭代、取消、关闭或转成 SSE |
| `AgUiResumeRequest` | 只把不可信客户端决定带入 checkpoint 解析 |
| `AgUiResumeBinding` | 由框架解析并交给 `new_agui(...)` 的私有恢复事实 |
| `AgUiResumeCheckpoint` | 原生 resume marker 已持久化的稳定证据 |
| `TINKERFIN_HITL_CONTRACT` | 外部 mixed-cancellation 子 Agent 的契约声明 |

完整 Runtime 参数见 [AG-UI 入门](index.md)，恢复参数见 [interrupt 与恢复](interrupts-and-resume.md)。

## 转换入口

| API | 主要参数 | 什么时候使用 |
| --- | --- | --- |
| `astream_events(...)` | `parts`、`identity`、公开开关、此前已发出的 Tool ID、私有 state key | 已有原生异步流，希望自动管理完整生命周期 |
| `DeepAgentAgUiAdapter(...)` | `identity`、此前已发出的 Tool ID、公开开关、私有 state key | 需要自己管理主开始和终止事件 |
| `encode_sse(...)` | `event`、可选 `event_id` | 把单个 AG-UI 事件编码为 SSE |
| `micro_batch(...)` | `events`、可选 batcher | 合并连续的小增量 |

### `DeepAgentAgUiAdapter` 方法

| 方法 | 作用 |
| --- | --- |
| `process(part)` | 校验并转换一条完整原生数据 |
| `finish()` | 正常结束仍开放的文字、推理和 Tool 生命周期 |
| `abort()` | 关闭开放的子生命周期；主终态仍由编排器唯一负责 |
| `main_outcome()` | 返回 success 或 interrupt 终止结果 |

## 生命周期工厂

`AgUiLifecycleEventFactory` 只适合自定义编排器。

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `started(...)` | `identity`、可选 `parent_run_id` | `RUN_STARTED`，input 缺省 |
| `finished(...)` | `identity`、`outcome` | 使用 canonical identity 的 `RUN_FINISHED` |
| `failed(...)` | `identity`、`message`、`code`、可选 `parent_run_id` | 使用 canonical identity 的 `RUN_ERROR` |
| `is_main_lifecycle(...)` | `event`、`identity` | 判断事件是否占用该主生命周期 |
| `event_run_id(event)` | 事件 | 读取可验证的 run ID |
| `validate_identity(...)` | `identity` | 提前验证运行身份类型 |

`AgentRunOutcome` 的 `type` 为 `success` 或 `interrupt`；只有 interrupt 结果携带 `interrupts`。

## 恢复类型

| API | 用途 |
| --- | --- |
| `DeepAgentDefinition.prepare_agui_resume(...)` | 从权威 checkpoint 恢复 pending 事实并构造高层 Binding |
| `AgUiResumeRequest` | 不可变、非空且拒绝重复 ID 的客户端恢复项 |
| `AgUiResumeBinding.from_agui(...)` | 自行拥有完整可信 AG-UI 终止日志时使用的高级入口 |
| `AgUiResumeBinding.model_validate(...)` | 恢复完整稳定 Binding JSON 模型 |
| `AgUiResumeBindingError` | 高层 Binding 无法无损保留恢复语义 |
| `ResumeMapper.map(...)` | 从原生 interrupt 和 checkpoint 消息转换恢复请求 |
| `ResumeMapper.map_agui(...)` | 从服务端保存的 AG-UI interrupt 转换恢复请求 |
| `ResumeTranslation` | 保存 kind、mode、恢复数据、取消项、Tool ID、来源与原生决定 |
| `ResumeMappingError` | Adapter 低层数据无法无损映射 |

`ResumeTranslation` 是 Adapter 的低层结果。高层 Runtime 把 `AgUiResumeRequest` 交给
`prepare_agui_resume(...)`，Definition 从 checkpointer 恢复原生 interrupt 与完整消息。返回的
Binding 表示全部 resolved、Tool mixed cancellation 或全部 cancelled abandonment，不公开原生
Command。`from_agui(...)` 只适用于可信事件日志集成，不能接收客户端重新提交的 interrupt 详情。

## interrupt 数据模型

| 模型 | 字段 |
| --- | --- |
| `AgentRuntimeInterrupt` | 非空 `id`、JSON `value` |
| `RuntimeInterruptEnvelope` | 当前 `schema`、非空 `kind`、可选 `message`、响应 JSON Schema 和可信 metadata |
| `HitlActionRequest` | 非空 `name`、对象 `args`、可选 `description` |
| `HitlReviewConfig` | `actionName`、非空 `allowedDecisions`、可选 `argsSchema` |
| `HitlRequest` | 等长且非空的 `actionRequests` 与 `reviewConfigs` |
| `ToolReviewInterruptMetadata` | 当前原生分组、action 位置、Tool 名称、决定与原始参数 |
| `SubagentProvenance` | 稳定 invocation ID、完整 namespace、graph task、父 Tool、Agent、描述和当前请求 run |

公开决定为 `approve`、`edit`、`reject`、`respond`。同一请求中的 action 和 review config
按位置配对。Adapter 在返回转换结果前，使用 JSON Schema Draft 2020-12 按 `argsSchema` 校验
编辑后的参数。

`RuntimeInterruptEnvelope` 用于非 Tool 工作流暂停。Adapter 把 `kind` 映射为 AG-UI
interrupt reason。`require_valid_schema(...)` 在发布前检查 Draft 2020-12 Schema，
`validate_json_schema_instance(...)` 在原生转换前使用 format checker 校验恢复 JSON；发出该
envelope 的 Graph 继续负责业务语义。扩展 kind 必须带命名空间，一个待处理批次不能同时包含
Runtime interrupt 和 Tool interrupt。

`parse_tool_review_interrupt(interrupt)` 按
`tinkerfin.deepagents.tool-review` 校验完整可信 Tool interrupt。
`subagent_invocation_id(...)` 和 `create_subagent_provenance(...)` 实现固定的
`tinkerfin.subagent-provenance` 身份契约。

## ID 与错误

| API | 作用 |
| --- | --- |
| `ScopedIdCodec.encode(...)` | 由类型、完整 namespace、原始 ID 创建 scoped ID |
| `ScopedIdCodec.decode(...)` | 还原 scoped ID 的三部分 |
| `HitlCorrelationError` | 审批动作与 Tool 消息无法可靠关联 |
| `ToolReviewContractError` | 完整 Tool review interrupt 不符合当前公开契约 |
| `SseEventId` | `encode_sse()` 接受的字符串或整数 ID 类型 |

并行工具、子 Agent 和恢复流程都必须使用完整 scoped ID，不能按事件到达顺序关联。

`ScopedIdCodec.encode()` 的 kind 可以是 `message`、`tool`、`reasoning` 或 `reasoning-message`；namespace 必须是只含非空字符串的元组，原始 ID 也不能为空。
