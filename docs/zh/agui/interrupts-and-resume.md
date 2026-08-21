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
| `plan_clarification` | `{"type":"respond","answers":[{"questionId":"...","answer":"...","optionId":"..."}]}`；自由输入时省略 `optionId` |
| `plan_review` | `approve`、`edit`、`respond` 或 `reject`，并携带当前 `baseRevision` |

Plan interrupt 没有 `toolCallId`，其中包含带版本的可信 Runtime envelope、响应 JSON Schema
和 Plan metadata。根状态的 `tinkerfin_plan` 会在 interrupt 终止事件前发布。恢复请求先同步
snapshot，随后可以用 RFC 6902 state delta 表示 Plan 进入 `executing` 和 `completed`。

Plan interrupt 使用同一套 `ResumeMapper.map_agui(...)` 和 `AgUiResumeBinding`，不需要
另一套恢复 API。`ResumeMapper` 校验已保存 envelope 和待处理项的完整覆盖，父 Graph 校验
响应契约，并拒绝过期的 `baseRevision`。每次恢复使用新的 `runId`，同时保持原
`threadId`。

同一待处理批次不能混合 Plan interrupt 与 Tool interrupt。Plan 批准后仍可能在执行阶段
产生 Tool 审批；后续恢复会继续使用原来的 scoped Tool ID。

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
from tinkerfin_agui_adapter import ResumeMapper


translation = ResumeMapper().map_agui(
    entries=resume_entries,
    interrupts=persisted_interrupts,
)

if translation.mode == "command":
    binding = AgUiResumeBinding.from_translation(
        identity=identity,
        translation=translation,
    )
```

`persisted_interrupts` 必须来自服务端可信的事件记录，不能使用客户端重新提交的 interrupt 详情。

接着创建 Runtime，并把恢复命令作为 Graph 输入：

```python
runtime = agent.new_agui(
    identity=identity,
    resume=binding,
)
events = runtime.astream(binding.command)
```

`AgUiResumeBinding` 绑定 `Identity`、纯 `Command(resume=...)` 和之前已经发出的 scoped Tool ID。完整 HTTP 请求的幂等与权限仍由应用校验。

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

## 三种转换结果

| `translation.mode` | 含义 | 应用怎么做 |
| --- | --- | --- |
| `command` | 所有决定都能转换成原生恢复命令 | 创建 binding 并继续 Graph |
| `abandon` | 所有项都被取消 | 结束本次业务流程，不伪造拒绝 |
| `custom` | 解决和取消混合，原生恢复不能无损表达 | 由应用决定如何处理；不要强行继续 |

取消表示用户放弃这次恢复，不等于拒绝工具。把取消改成拒绝会改变业务语义。
Plan 审批同样如此：取消不能伪造成拒绝计划。

## 重试和并发

- 为恢复请求使用新的 `runId`，但保持相同 `threadId`；
- 在业务存储中原子地认领待处理 interrupt，避免两个请求同时恢复；
- 保存转换后的恢复数据和完整 Tool ID，重试时重建同一个 binding；
- 恢复后的 Tool 结果沿用原 Tool ID，不重复发送 Tool start、args 和 end。

## 常见错误

| 错误 | 常见原因 |
| --- | --- |
| `ResumeMappingError` | ID 不存在、覆盖不完整、决定不允许或缺少 Tool 关联数据 |
| `ValueError` | binding 使用了空 resume、非纯恢复命令或不完整 Tool ID |
| 恢复后找不到状态 | `Identity.threadId` 改变，或没有配置 checkpointer |
| 同一个操作执行两次 | 应用没有原子认领 interrupt，或重试没有复用已保存的恢复数据 |

下一篇：[只使用 AG-UI 转换器](adapter-extensions.md)。
