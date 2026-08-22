# AG-UI 使用参考

[AG-UI 入门](index.md) · [English](../../en/agui/api-reference.md)

## 高层 Runtime 能力

| API | 用途 |
| --- | --- |
| `DeepAgentDefinition.new_agui(...)` | 创建一次 AG-UI Runtime |
| `DeepAgentAgUiRuntime.astream(...)` | 运行 Graph 并得到 `AgUiEventStream` |
| `AgUiEventStream` | 迭代、取消、关闭或转成 SSE |
| `AgUiResumeBinding` | 把恢复请求、原生命令和旧 Tool ID 绑定在一起 |

完整 Runtime 参数见 [AG-UI 入门](index.md)，恢复参数见 [interrupt 与恢复](interrupts-and-resume.md)。

## 转换入口

| API | 主要参数 | 什么时候使用 |
| --- | --- | --- |
| `astream_events(...)` | `parts`、`identity`、公开开关、旧 Tool ID、私有 state key | 已有原生异步流，希望自动管理完整生命周期 |
| `DeepAgentAgUiAdapter(...)` | `identity`、旧 Tool ID、公开开关、私有 state key | 需要自己管理主开始和终止事件 |
| `encode_sse(...)` | `event`、可选 `event_id` | 把单个 AG-UI 事件编码为 SSE |
| `micro_batch(...)` | `events`、可选 batcher | 合并连续的小增量 |

### `DeepAgentAgUiAdapter` 方法

| 方法 | 作用 |
| --- | --- |
| `process(part)` | 校验并转换一条完整原生数据 |
| `finish()` | 正常结束仍开放的文字、推理和 Tool 生命周期 |
| `abort(code=...)` | 失败或取消时关闭开放生命周期 |
| `main_outcome()` | 返回 success 或 interrupt 终止结果 |

## 生命周期工厂

`AgUiLifecycleEventFactory` 只适合自定义编排器。

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `started(...)` | `identity` | `RUN_STARTED`，`input=None` |
| `finished(...)` | `identity`、`outcome` | `RUN_FINISHED` |
| `failed(...)` | `identity`、`message`、`code` | `RUN_ERROR` |
| `is_main_lifecycle(...)` | `event`、`identity` | 判断事件是否占用该主生命周期 |
| `event_run_id(event)` | 事件 | 读取可验证的 run ID |
| `validate_identity(...)` | `identity` | 提前验证运行身份类型 |

`AgentRunOutcome` 的 `type` 为 `success` 或 `interrupt`；只有 interrupt 结果携带 `interrupts`。

## 恢复类型

| API | 用途 |
| --- | --- |
| `ResumeMapper.map(...)` | 从原生 interrupt 和 checkpoint 消息转换恢复请求 |
| `ResumeMapper.map_agui(...)` | 从服务端保存的 AG-UI interrupt 转换恢复请求 |
| `ResumeTranslation` | 保存 `mode`、恢复数据、取消 ID、旧 Tool ID 和逐 interrupt 决定 |
| `ResumeMappingError` | 恢复请求无法无损映射 |
| `ResumeMappingFailure` | 稳定的失败类别 |

`ResumeTranslation.mode` 只能是 `command`、`abandon` 或 `custom`。

## interrupt 数据模型

| 模型 | 字段 |
| --- | --- |
| `AgentRuntimeInterrupt` | 非空 `id`、JSON `value` |
| `RuntimeInterruptEnvelope` | 带版本的 `schema`、非空 `kind`、可选 `message`、响应 JSON Schema 和可信 metadata |
| `HitlActionRequest` | 非空 `name`、对象 `args`、可选 `description` |
| `HitlReviewConfig` | `actionName`、非空 `allowedDecisions`、可选 `argsSchema` |
| `HitlRequest` | 等长且非空的 `actionRequests` 与 `reviewConfigs` |
| `ToolReviewInterruptMetadata` | 带版本的原生分组、action 位置、Tool 名称、决定与原始参数 |
| `SubagentProvenance` | 稳定 invocation ID、完整 namespace、graph task、父 Tool、Agent、描述和当前请求 run |
| `ToolResultCorrelation` | 父图完成子图 Tool 时使用的原 scoped Tool 与父消息 ID |

允许的决定为 `approve`、`edit`、`reject`、`respond`。同一请求中的 action 和 review config 按位置配对。

`RuntimeInterruptEnvelope` 用于非 Tool 工作流暂停。Adapter 把 `kind` 映射为 AG-UI
interrupt reason，发出该 envelope 的 Graph 负责校验恢复 JSON 的业务语义。一个待处理批次
不能同时包含 Runtime interrupt 和 Tool interrupt。

`parse_tool_review_interrupt(interrupt)` 按
`tinkerfin.deepagents.tool-review.v1` 校验完整可信 Tool interrupt。
`subagent_invocation_id(...)` 和 `create_subagent_provenance(...)` 实现固定的
`tinkerfin.subagent-provenance.v1` 身份契约。

## ID 与错误

| API | 作用 |
| --- | --- |
| `ScopedIdCodec.encode(...)` | 由类型、完整 namespace、原始 ID 创建 scoped ID |
| `ScopedIdCodec.decode(...)` | 还原 scoped ID 的三部分 |
| `HitlCorrelationError` | 审批动作与 Tool 消息无法可靠关联 |
| `ToolReviewContractError` | 完整 Tool review interrupt 不符合带版本的公开契约 |
| `SseEventId` | `encode_sse()` 接受的字符串或整数 ID 类型 |

并行工具、子 Agent 和恢复流程都必须使用完整 scoped ID，不能按事件到达顺序关联。

`ScopedIdCodec.encode()` 的 kind 可以是 `message`、`tool`、`reasoning` 或 `reasoning-message`；namespace 必须是只含非空字符串的元组，原始 ID 也不能为空。
