# interrupt 与恢复

[看懂 AG-UI 事件](events.md) · [English](../../en/agui/interrupts-and-resume.md)

当 Agent 准备执行需要人工确认的操作时，可以暂停运行并返回 interrupt。用户作出决定后，再用同一个 thread 的 checkpoint 继续执行。

## 先让工具需要审批

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[delete_order],
    interrupt_on={
        "delete_order": {
            "allowed_decisions": ["approve", "reject"],
        }
    },
    checkpointer=MemorySaver(),
)
```

没有 checkpointer 时，暂停后的 Graph 没有可靠状态可以恢复。

## Plan 需求澄清和审批

通过 `TinkerFin().plan(enabled=True)` 创建的 Agent 还会因为两类 Runtime 原因暂停。本次
请求使用 `mode="plan"`：

```python
events = await tinkerfin.open_agui_run(
    identity,
    agent=agent,
    input=graph_input,
    mode="plan",
)
```

| `reason` | `resolved` 时的 payload |
| --- | --- |
| `tinkerfin:plan_clarification` | 使用 `{"type":"respond","answers":{"question-id":{"status":"answered","answerType":"single_choice","optionId":"option-id"}}}`；每道题的 value 服从 interrupt 的精确响应 Schema，可选题跳过使用 `{"status":"skipped"}` |
| `tinkerfin:plan_review` | 使用 interrupt 响应 Schema 允许的动作并携带当前 `baseRevision`；默认是 `approve`、`respond` 或 `reject`，`edit` 需要宿主显式配置 |

Plan interrupt 没有 `toolCallId`，其中包含已声明的可信 Runtime envelope、响应 JSON Schema
和携带完整公开 Form 的 Plan clarification metadata。
每道问题显式携带 `answerType` 和 `required`；内置题型为 `single_choice`、
`multiple_choice`、`text` 和 `date`。
Question/Option attributes 会作为公开、
非权威的规划参考原样保留；内部 Schema fingerprint 只存在于 checkpoint，不属于公开
AG-UI 事件。根状态的 `tinkerfin_plan` 会在 interrupt 终止事件前发布。恢复请求先同步
snapshot，随后可以用 RFC 6902 state delta 表示 Plan 进入 `approved`，并在原生执行前发布
`effectiveMode=default`。

客户端不能回传 Form、label、description 或 attributes。Planning Graph 从 checkpoint 恢复可信
Form 并派生所选 Option label。`answers` object 必须完整覆盖每个 checkpoint question ID，且不能
包含未知 key。提交其他 `answerType` 的字段、违反多选数量、无效日期、跳过必填题或使用未知 Option
ID 都会在 Graph 消费 resume 前失败。

Plan 与 Tool interrupt 使用同一套 `open_agui_run(resume=...)`。框架从权威 checkpoint 恢复可信
envelope 和完整待处理集合，Planning Graph 校验响应契约，并拒绝过期的 `baseRevision`。每次
恢复使用新的 `runId`，同时保持原 `threadId`。

同一待处理批次不能混合 Plan interrupt 与 Tool interrupt。Plan 批准后仍可能在执行阶段
产生 Tool 审批；后续恢复会继续使用原来的 scoped Tool ID。

已发布 Tool 审批的 `metadata.deepagents` 使用
`tinkerfin.deepagents.tool-review`，必填字段为 `nativeInterruptId`、
`actionIndex`、`toolName`、`allowedDecisions` 和 `originalArgs`。高层 Runtime 不要求宿主持久化
或回传这段 metadata，而是从 checkpointer 恢复并校验原生 interrupt。直接使用 Adapter 且自行
维护可信事件日志的集成，可以用 `parse_tool_review_interrupt()` 校验完整的已发布 interrupt。
缺失字段、未知字段、非法决定或与 `metadata.langgraphValue` 不一致都会失败关闭。

取消 Plan 澄清或审阅表示放弃当前 Plan 请求，不能伪造成 `reject`。后续普通输入可以在
同一个 Plan-capable Definition 和 checkpoint thread 上使用 `mode="default"`。修改未来
mode 不会批准、拒绝或取消待处理的 Tool/Filesystem 审批。

## 前端提交恢复决定

恢复请求的 `resume` 是一个列表。每一项对应一个待处理 interrupt。

```json
{
  "interruptId": "interrupt-1",
  "status": "resolved",
  "payload": {"type": "approve"}
}
```

| 字段 | 必填 | 作用 |
| --- | --- | --- |
| `interruptId` | 是 | 前一次终止事件给出的 interrupt ID |
| `status` | 是 | `resolved` 表示已决定，`cancelled` 表示放弃 |
| `payload` | 否 | 决定内容，例如批准、编辑、拒绝或回答 |

服务端应读取当前完整的待处理集合，并要求恢复请求一次覆盖全部待处理项。不要只凭前端传来的 interrupt 内容恢复。

## 从权威 checkpoint 恢复

普通宿主只把客户端决定交给框架。Managed 门面会从同一 checkpoint thread 读取当前 pending
interrupt、完整消息、lineage 和 Runtime Profile：

```python
from tinkerfin import AgUiResumeRequest


events = await tinkerfin.open_agui_run(
    resume_identity,
    agent=agent,
    resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    parent_run_id=parent_run_id,
    config=config,
    on_resume_saved=record_checkpoint_idempotently,
    on_resume_not_saved=release_unprepared_claim_idempotently,
)
```

`AgUiResumeRequest` 会拒绝重复 ID，且不包含服务端 interrupt payload、原生 Command、checkpoint
身份或 Runtime Profile 选择。解析阶段要求完整覆盖 pending 集合，并使用 checkpoint 中的完整
消息证明 Tool 关联。未知、过期、缺失或不允许的决定都会在 Graph 继续前失败。应用仍需校验
完整 HTTP 请求与权限，并原子认领公开 pending 集合。

## Adapter 高级恢复入口

`prepare_agui_resume()` 与 `AgUiResumeBinding` 继续作为自定义编排使用的高级 Definition 边界。
自行拥有完整原生 checkpoint 对象的 Adapter 集成可以使用低层 mapper：

```python
translation = ResumeMapper().map(
    entries=resume_entries,
    interrupts=pending_interrupts,
    messages_by_namespace=messages_by_namespace,
)
```

已处理的决定需要完整消息来关联 Tool。多个同名工具或并行工具不能按到达顺序匹配。
这是 Adapter 层的检查入口；普通 Runtime 调用方不需要翻译或传入原生 Command。

如果高级集成自行拥有可信、完整的 AG-UI 终止事件日志，也可以使用
`AgUiResumeBinding.from_agui(entries=..., interrupts=...)` 直接校验这些已保存的公开事实。它绝不
能接收客户端重新提交的 interrupt 详情，也不是标准 Definition/Checkpointer 流程的必需步骤。

## 三种恢复模式

| 模式 | 含义 | Runtime 行为 |
| --- | --- | --- |
| 全部 resolved | 所有项都能转换成原生恢复数据 | checkpoint 一次并继续 Graph |
| 全部 cancelled | 所有项都被放弃 | 不调用 Graph，输出有限 cancelled 生命周期 |
| mixed | 同时有 resolved 与 cancelled | 执行 resolved Tool，结算但不执行 cancelled slot |

`cancelled` 永远不会转成 reject。Tool 混合批次中，TinkerFin 会执行 resolved Tool，并为每个
cancelled Tool 生成确定性的未执行 error `ToolMessage`。main、general-purpose、声明式子 Agent
和权限生成的审批使用同一适配器。通用 Runtime 的 mixed interrupt 不是 Tool decision，
`AgUiResumeBinding` 会拒绝它。

提交原生 decision 前，所选 Runtime Profile 会先在精确的 interrupted checkpoint 上持久写入
私有 lineage 与 marker；这个阶段不执行 Graph node，也不替换 root、Planning 或子图的 pending
work。只有这些写入可读后，框架才会在首条恢复后原生事件公开前调用
`on_resume_saved`。prepared 重试会再次交付同一个 `AgUiResumeCheckpoint`，因此回调必须
幂等；Graph 已接受的 decision 不会重复提交。decision-only 与 `None` continuation 都留在框架内部。

如果请求解析、staging 或流启动在 marker 可读前失败或取消，Runtime 会通过受保护结算调用
`on_resume_not_saved`。宿主用这个幂等回调释放已认领的公开 interrupt 集合；prepared 或
accepted 证据存在后不会调用，重试继续通过 `on_resume_saved` 结算。

## 重试和并发

- 为恢复请求使用新的 `runId`，但保持相同 `threadId`；
- 在业务存储中原子地认领待处理 interrupt，避免两个请求同时恢复；
- 在 checkpoint 结算前保持客户端决定请求不变，重试时不得重新构造或接受客户端提供的
  interrupt metadata；
- 只根据 `on_resume_saved` 解决业务审批，不能根据 `RUN_STARTED`；
- 恢复后的 Tool 结果沿用原 Tool ID，不重复发送 Tool start、args 和 end。

## 常见错误

| 错误 | 常见原因 |
| --- | --- |
| `AgUiResumeBindingError` | ID 不存在、覆盖不完整、决定不允许、Schema payload 非法或缺少 Tool 关联数据 |
| `ValueError` | input/resume 选择非法、通用 Runtime mixed cancellation 不支持或 Tool ID 不完整 |
| 恢复后找不到状态 | `RunIdentity.threadId` 改变，或没有配置 checkpointer |
| 同一个操作执行两次 | 应用没有原子认领 interrupt，或重试没有复用已保存的恢复数据 |

下一篇：[只使用 AG-UI 转换器](adapter-extensions.md)。
