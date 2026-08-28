# AG-UI 入门

[文档首页](../README.md) · [English](../../en/agui/index.md)

AG-UI 把 Agent 的文字、工具调用、状态、审批和结果表示成前端事件。TinkerFin 使用一个
canonical `RunIdentity` 统一公开生命周期、Graph、checkpoint、协调与持久投递。

## 安装

```bash
pip install "tinkerfin[agui]"
pip install langchain-openai
```

第二条命令只安装示例使用的模型适配包；使用其他供应商时应替换为对应依赖。

## 第一个 AG-UI Runtime

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new_agui(
        identity=RunIdentity(threadId="conversation-1", runId="run-1"),
    )
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "你好"}]},
    )
    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `RunIdentity.threadId` 是所有 Runtime 与持久化边界共用的 canonical thread
- `RunIdentity.runId` 标识一次语义运行，只在幂等重试时复用
- `runtime.astream(...)` 的第一个参数是明确的 Graph 输入
- `RUN_STARTED.input` 缺省；Runtime 不制造或重复 Graph 输入

## HTTP 输入与 Graph 输入

前端仍可发送标准 `RunAgentInput`。应用在 HTTP 边界校验后，只映射已经授权的事实：

| `RunAgentInput` 字段 | 应用责任 | 框架调用 |
| --- | --- | --- |
| `threadId` / `runId` | 认证、授权并选择唯一 canonical identity | `RunIdentity(...)` |
| `parentRunId` | 授权同 thread 的分支或恢复来源 | `parent_run_id=...` |
| `state` / `messages` | 校验并映射到具体 Graph state schema | `astream(graph_input)` |
| `tools` | 只作为客户端描述，不能授予服务端执行权限 | 不自动传入 |
| `context` | 仅在宿主明确支持时转换 | Graph `context=...` |
| `forwardedProps` | 执行模型、mode 等产品策略 | 应用负责 |
| `resume` | 只携带客户端决定 | `AgUiResumeRequest(entries=...)` |

已有 checkpoint 时不要再次注入完整前端历史，否则同一条消息可能执行两次。

## Resume

```python
from tinkerfin import AgUiResumeRequest, RunIdentity


resume_identity = RunIdentity(threadId="conversation-1", runId="run-resume")
binding = await agent.prepare_agui_resume(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    request=AgUiResumeRequest(entries=tuple(resume_entries)),
)
runtime = agent.new_agui(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
    on_resume_initialization_failed=release_unprepared_claim_idempotently,
)
events = runtime.astream(config=config)
```

Definition 从权威 checkpoint 读取事实，并校验完整覆盖、原生 group、decision 顺序、
JSON Schema、Tool 关联、取消、Runtime Profile 和子 Agent 来源后，才返回私有 binding。
客户端请求不包含服务端 interrupt payload 或原生 `Command`。全部 cancelled 的批次不会调用 Graph。

所选 Profile 会先写入私有 lineage 与 marker，但不改变 interrupted Graph 的 pending work。
`on_resume_checkpointed` 在这些值可读后、恢复后的原生输出之前调用。prepared 重试可能再次
收到同一个 `AgUiResumeCheckpoint`，因此回调必须幂等；它不表示 Tool 或 run 已完成。
如果 Runtime 在 marker 可读前失败、取消或关闭，受保护结算会调用
`on_resume_initialization_failed`，供宿主释放认领；prepared 或 accepted marker 已存在后不会调用。

## `new_agui()` 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | canonical thread 与 run 身份 |
| `parent_run_id` | `None` | 可选 checkpoint 分支或恢复来源 |
| `mode` | Definition 默认值 | 当前 Runtime 的 `default` 或 `plan` |
| `on_part` | `None` | 观察每条已验证上游 Native 对象 |
| `timeout` | `None` | 整条原生流的总时限 |
| `settlement_timeout` | `None` | 调用方等待受保护流清理的时限 |
| `expose_reasoning_events` | `False` | 输出支持的公开推理事件 |
| `expose_subagent_events` | `True` | 输出已验证子 Agent 事件 |
| `resume` | `None` | 已验证 `AgUiResumeBinding` |
| `on_resume_checkpointed` | `None` | 确切 resume-intent marker 可读后的幂等回调 |
| `on_resume_initialization_failed` | `None` | 只在 marker 持久前执行的宿主幂等结算 |
| `on_event` | `None` | 每条 AG-UI 事件交付前的异步观察函数 |

推理开关不会放行 provider 私有元数据。

## Profile 管理的流设置

所选 Runtime Profile 负责必需语义 mode、上游 version、subgraph 行为与完整 state 输出，
通常不需要填写这些参数。当前内置 Profile 允许增加受支持的诊断 mode；移除必需语义或传入
冲突的上游参数，会在 Graph 迭代前失败。AG-UI 转换消费 Runtime Observation 已经使用的同一个
canonical frame。

`parent_run_id` 不是子 Agent 谱系。它在同一 canonical thread 中选择该 run 的唯一有效
checkpoint 叶节点。父 run 缺失、跨 thread、仍活跃、已失败、存在歧义、自引用，或暂停后
没有匹配 resume 时，框架会在 Graph 执行前拒绝请求。

## 下一步

- [看懂 AG-UI 事件](events.md)
- [interrupt 与恢复](interrupts-and-resume.md)
- [只使用转换器](adapter-extensions.md)
- [AG-UI 使用参考](api-reference.md)
