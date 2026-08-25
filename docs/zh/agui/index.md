# AG-UI 入门

[文档首页](../README.md) · [English](../../en/agui/index.md)

AG-UI 把 Agent 的文字、工具调用、状态、审批和结果表示成前端事件。TinkerFin 使用一个
canonical `Identity` 统一公开生命周期、Graph、checkpoint、协调与持久投递。

## 第一个 AG-UI Runtime

```python
import asyncio

from tinkerfin import Identity, TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new_agui(
        identity=Identity(threadId="conversation-1", runId="run-1"),
    )
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "你好"}]},
    )
    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `Identity.threadId` 是所有 Runtime 与持久化边界共用的 canonical thread
- `Identity.runId` 标识一次语义运行，只在幂等重试时复用
- `runtime.astream(...)` 的第一个参数是明确的 Graph 输入
- `RUN_STARTED.input` 缺省；Runtime 不制造或重复 Graph 输入

## HTTP 输入与 Graph 输入

前端仍可发送标准 `RunAgentInput`。应用在 HTTP 边界校验后，只映射已经授权的事实：

| `RunAgentInput` 字段 | 应用责任 | 框架调用 |
| --- | --- | --- |
| `threadId` / `runId` | 认证、授权并选择唯一 canonical identity | `Identity(...)` |
| `parentRunId` | 授权同 thread 的分支或恢复来源 | `parent_run_id=...` |
| `state` / `messages` | 校验并映射到具体 Graph state schema | `astream(graph_input)` |
| `tools` | 只作为客户端描述，不能授予服务端执行权限 | 不自动传入 |
| `context` | 仅在宿主明确支持时转换 | Graph `context=...` |
| `forwardedProps` | 执行模型、mode 等产品策略 | 应用负责 |
| `resume` | 与服务端保存的可信 interrupt 配对 | `AgUiResumeBinding.from_agui(...)` |

已有 checkpoint 时不要再次注入完整前端历史，否则同一条消息可能执行两次。

## Resume

```python
from tinkerfin import AgUiResumeBinding, Identity


binding = AgUiResumeBinding.from_agui(
    entries=resume_entries,
    interrupts=trusted_persisted_interrupts,
)
runtime = agent.new_agui(
    identity=Identity(threadId="conversation-1", runId="run-resume"),
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
)
events = runtime.astream(config=config)
```

Binding 校验完整覆盖、原生 group、decision 顺序、JSON Schema、Tool 关联、取消和子 Agent
来源。它具有稳定 JSON 往返，但不持有 identity、parent、Graph 或 I/O 资源。原生
`Command` 只由 Runtime 内部构造。全部 cancelled 的批次会产生有限的 cancelled 生命周期，
不会调用 Graph。

`on_resume_checkpointed` 在私有 marker 可读后、恢复后的原生输出之前调用。重试可能再次
收到同一个 `AgUiResumeCheckpoint`，因此回调必须幂等；它不表示 Tool 或 run 已完成。

## `new_agui()` 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | canonical thread 与 run 身份 |
| `parent_run_id` | `None` | 可选 checkpoint 分支或恢复来源 |
| `mode` | Definition 默认值 | 当前 Runtime 的 `default` 或 `plan` |
| `on_part` | `None` | 观察每条已验证 LangGraph v2 数据 |
| `timeout` | `None` | 整条原生流的总时限 |
| `settlement_timeout` | `None` | 调用方等待受保护流清理的时限 |
| `expose_reasoning_events` | `False` | 输出支持的公开推理事件 |
| `expose_subagent_events` | `True` | 输出已验证子 Agent 事件 |
| `resume` | `None` | 已验证 `AgUiResumeBinding` |
| `on_resume_checkpointed` | `None` | resume 持久接受后的幂等回调 |
| `on_event` | `None` | 每条 AG-UI 事件交付前的异步观察函数 |

推理开关不会放行 provider 私有元数据。

## Runtime 固定的流设置

AG-UI 转换要求 `messages/tasks/values`、`version="v2"` 与 `subgraphs=True`。通常直接
省略；可额外增加 `updates`、`checkpoints`、`debug` 或 `custom`。缺少必需模式、使用 v1
或关闭 subgraphs 会在 Graph 迭代前失败。

`parent_run_id` 不是子 Agent 谱系。它在同一 canonical thread 中选择该 run 的唯一有效
checkpoint 叶节点。父 run 缺失、跨 thread、仍活跃、已失败、存在歧义、自引用，或暂停后
没有匹配 resume 时，框架会在 Graph 执行前拒绝请求。

## 下一步

- [看懂 AG-UI 事件](events.md)
- [interrupt 与恢复](interrupts-and-resume.md)
- [只使用转换器](adapter-extensions.md)
- [AG-UI 使用参考](api-reference.md)
