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
请求需要在创建 Runtime 时选择 `mode="plan"`：

```python
runtime = agent.new_agui(identity=identity, mode="plan")
```

| `reason` | `resolved` 时的 payload |
| --- | --- |
| `tinkerfin:plan_clarification` | 选择 Option 时使用 `{"type":"respond","answers":[{"questionId":"...","optionId":"..."}]}`；自由输入时使用 `{"type":"respond","answers":[{"questionId":"...","answer":"..."}]}`；跳过可选题时使用 `{"type":"respond","answers":[{"questionId":"...","skipped":true}]}` |
| `tinkerfin:plan_review` | `approve`、`edit`、`respond` 或 `reject`，并携带当前 `baseRevision` |

Plan interrupt 没有 `toolCallId`，其中包含带版本的可信 Runtime envelope、响应 JSON Schema
和携带完整公开 Form 的 `tinkerfin.plan-clarification.v2` metadata；Form 使用 `schemaVersion: 2`。
Python 的 `allow_free_text` 在 JSON 中表示为 `allowFreeText`，每道问题还会显式携带 `required`。
Question/Option attributes 会作为公开、
非权威的规划参考原样保留；内部 Schema fingerprint 只存在于 checkpoint，不属于公开
AG-UI 事件。根状态的 `tinkerfin_plan` 会在 interrupt 终止事件前发布。恢复请求先同步
snapshot，随后可以用 RFC 6902 state delta 表示 Plan 进入 `approved`，并在原生执行前发布
`effectiveMode=default`。

客户端不能回传 Form、label、description 或 attributes。Planning Graph 从 checkpoint 恢复可信
Form 并派生所选 Option label。Resume 必须完整覆盖每道问题；混合回答字段、跳过必填题、漏传问题
或使用未知 ID 都会导致恢复失败。

Plan interrupt 使用同一套 `AgUiResumeBinding.from_agui(...)`，不需要另一套恢复 API。
Binding 校验已保存 envelope 和待处理项的完整覆盖，Planning Graph 校验响应契约，并拒绝过期
的 `baseRevision`。每次恢复使用新的 `runId`，同时保持原 `threadId`。

同一待处理批次不能混合 Plan interrupt 与 Tool interrupt。Plan 批准后仍可能在执行阶段
产生 Tool 审批；后续恢复会继续使用原来的 scoped Tool ID。

Tool 审批的 `metadata.deepagents` 使用
`tinkerfin.deepagents.tool-review.v1`，必填字段为 `nativeInterruptId`、
`actionIndex`、`toolName`、`allowedDecisions` 和 `originalArgs`。宿主应使用
`parse_tool_review_interrupt()` 解析服务端保存的完整 interrupt，并原样持久化。缺失字段、未知
字段、非法决定或与 `metadata.langgraphValue` 不一致都会失败关闭；不存在无版本 metadata 形状。

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

## 使用已保存的 AG-UI interrupt 恢复

如果服务端保存了前一次终止事件中的完整 interrupt，可以直接使用已经验证过的 Tool 关联：

```python
from tinkerfin import AgUiResumeBinding


binding = AgUiResumeBinding.from_agui(
    entries=resume_entries,
    interrupts=persisted_interrupts,
)
```

`persisted_interrupts` 必须来自服务端可信的事件记录，不能使用客户端重新提交的 interrupt 详情。
`from_agui()` 与公开解析入口复用同一个 Tool review v1 校验，并为完整恢复、混合取消或全部
放弃返回一个 Binding。

Binding 只传一次，原生 Command 由 Runtime 管理：

```python
runtime = agent.new_agui(
    identity=identity,
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
)
events = runtime.astream(config=config)
```

`AgUiResumeBinding` 保存原生 interrupt group、scoped Tool ID、取消和已验证的子 Agent 来源。
它具有稳定 JSON 往返，但不保存 identity 或 parent，也不暴露 `Command`。完整 HTTP 请求的幂等
与权限仍由应用校验。

## 使用原生 checkpoint 数据恢复

如果没有保存 AG-UI interrupt，但能读取当前 checkpoint，可以使用原生 interrupt 和按完整 namespace 分组的消息：

```python
translation = ResumeMapper().map(
    entries=resume_entries,
    interrupts=pending_interrupts,
    messages_by_namespace=messages_by_namespace,
)
```

已处理的决定需要完整消息来关联 Tool。多个同名工具或并行工具不能按到达顺序匹配。
这是 Adapter 层的检查入口。高层 Runtime 使用已保存 AG-UI interrupt 和
`AgUiResumeBinding.from_agui(...)`，调用方不需要翻译或传入原生 Command。

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

框架会把原生 resume 与私有 marker 原子写入 checkpoint。只有 marker 可读后，才会在首条
恢复后原生事件公开前调用 `on_resume_checkpointed`。重试可能再次收到相同
`AgUiResumeCheckpoint`，因此回调必须幂等。marker retry 和 `None` continuation 都留在框架
内部，调用方不需要第二次传入 input。

## 重试和并发

- 为恢复请求使用新的 `runId`，但保持相同 `threadId`；
- 在业务存储中原子地认领待处理 interrupt，避免两个请求同时恢复；
- 保存原始 AG-UI entries 和完整可信 interrupt，或保存 Binding 的完整稳定 JSON；不能选择性
  保存内部字段；
- 只根据 `on_resume_checkpointed` 解决业务审批，不能根据 `RUN_STARTED`；
- 恢复后的 Tool 结果沿用原 Tool ID，不重复发送 Tool start、args 和 end。

## 常见错误

| 错误 | 常见原因 |
| --- | --- |
| `AgUiResumeBindingError` | ID 不存在、覆盖不完整、决定不允许、Schema payload 非法或缺少 Tool 关联数据 |
| `ValueError` | binding 模式非法、通用 Runtime mixed cancellation 不支持或 Tool ID 不完整 |
| 恢复后找不到状态 | `Identity.threadId` 改变，或没有配置 checkpointer |
| 同一个操作执行两次 | 应用没有原子认领 interrupt，或重试没有复用已保存的恢复数据 |

下一篇：[只使用 AG-UI 转换器](adapter-extensions.md)。
