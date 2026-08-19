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

## 前端提交恢复决定

恢复请求的 `resume` 是一个列表。每一项对应一个待处理 interrupt。

```json
{
  "interruptId": "interrupt-1",
  "status": "resolved",
  "payload": {"decision": "approve"}
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
    entries=run_input.resume or (),
    interrupts=persisted_interrupts,
)

if translation.mode == "command":
    binding = AgUiResumeBinding.from_translation(
        run_input=run_input,
        translation=translation,
    )
```

`persisted_interrupts` 必须来自服务端可信的事件记录，不能使用客户端重新提交的 interrupt 详情。

接着创建 Runtime，并把恢复命令作为 Graph 输入：

```python
runtime = agent.new_agui(
    run_input=run_input,
    resume=binding,
)
events = runtime.astream(
    binding.command,
    {"configurable": {"thread_id": run_input.thread_id}},
)
```

`AgUiResumeBinding` 会检查 `run_input`、恢复命令和之前的 Tool ID 属于同一次恢复，避免把决定应用到错误的运行。

## 使用原生 checkpoint 数据恢复

如果没有保存 AG-UI interrupt，但能读取当前 checkpoint，可以使用原生 interrupt 和按完整 namespace 分组的消息：

```python
translation = ResumeMapper().map(
    entries=run_input.resume or (),
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
| 恢复后找不到状态 | `thread_id` 改变，或没有配置 checkpointer |
| 同一个操作执行两次 | 应用没有原子认领 interrupt，或重试没有复用已保存的恢复数据 |

下一篇：[只使用 AG-UI 转换器](adapter-extensions.md)。

