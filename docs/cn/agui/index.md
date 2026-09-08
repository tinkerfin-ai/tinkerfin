# AG-UI 入门

[文档首页](../index.md) · [English](../../en/agui/index.md)

AG-UI 把 Agent 文字、工具调用、状态、审批和结果表示成前端事件。TinkerFin 使用一个 canonical
`RunIdentity` 统一公开生命周期、Graph、checkpoint、协调与持久投递。

## 安装

```bash
pip install "tinkerfin[agui]"
pip install langchain-openai
```

第二条命令只安装下方示例使用的模型适配包；其他供应商应换成对应依赖。

## 第一次 managed AG-UI 运行

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    events = await tinkerfin.open_agui_run(
        RunIdentity(threadId="conversation-1", runId="run-1"),
        agent=agent,
        input={"messages": [{"role": "user", "content": "你好"}]},
    )
    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `RunIdentity.threadId` 是 canonical checkpoint thread
- `RunIdentity.runId` 标识一次语义运行，只在该运行重试时复用
- `input` 是明确的 Graph 输入
- `RUN_STARTED.input` 缺省；Runtime 不制造或重复输入
- 返回的 `AgUiEventStream` 只能使用一次，并拥有请求取消与清理

## HTTP 输入与 Graph 输入

前端可以发送标准 `RunAgentInput`。应用在 HTTP 边界校验后，只映射已经授权的事实：

| `RunAgentInput` 字段 | 应用责任 | 框架调用 |
| --- | --- | --- |
| `threadId` / `runId` | 认证、授权并选择唯一身份 | `RunIdentity(...)` |
| `parentRunId` | 授权同 thread 的分支或恢复来源 | `parent_run_id=...` |
| `state` / `messages` | 校验并映射到具体 Graph state | `input=graph_input` |
| `tools` | 只作为客户端描述，不能授予执行权限 | 不自动传入 |
| `context` | 仅在宿主明确支持时转换 | `context=...` |
| `forwardedProps` | 执行模型、mode 等产品策略 | 应用负责 |
| `resume` | 只携带客户端决定 | `resume=AgUiResumeRequest(...)` |

已有 checkpoint 时不要再次注入完整前端历史，否则同一消息可能执行两次。

## Resume

```python
from tinkerfin import AgUiResumeRequest, RunIdentity


events = await tinkerfin.open_agui_run(
    RunIdentity(threadId="conversation-1", runId="run-resume"),
    agent=agent,
    resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    parent_run_id=parent_run_id,
    config=config,
    on_resume_saved=record_checkpoint_idempotently,
    on_resume_not_saved=release_unprepared_claim_idempotently,
)
```

门面从权威 checkpoint 读取事实，并校验完整覆盖、原生 group、decision 顺序、JSON Schema、
Tool 关联、取消、Runtime Profile 和子 Agent 来源。客户端输入不包含服务端 interrupt payload 或
原生 `Command`。全部 cancelled 的批次输出有限 cancelled 生命周期，不调用 Graph。

所选 Profile 会写入私有 lineage 与 marker，但不改变 root、Planning 或 subgraph pending work。
`on_resume_saved` 在 marker 可读后调用，重试时可能再次收到同一个 `AgUiResumeCheckpoint`，因此
必须幂等。setup 在任何 prepared 或 accepted marker 出现前失败时，受保护结算会调用
`on_resume_not_saved`；已有 durable marker 证据后绝不调用。

## `open_agui_run()` 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | canonical thread 与 run 身份 |
| `agent` | 必填 | Definition，或返回 Definition 的同步/异步 callable |
| `input` / `resume` | 严格二选一 | 普通 Graph 输入或客户端决定 |
| `parent_run_id` | `None` | 可选 checkpoint 分支或恢复来源 |
| `mode` | Definition 默认值 | `default` 或 `plan` |
| `config` / `context` | `None` | Graph 配置与声明的 Runtime context |
| `stream_timeout` | `None` | 原生 pull 总时限 |
| `cleanup_timeout` | `None` | 调用方等待受保护清理的时限 |
| `include_reasoning_events` | `False` | 输出已验证的公开推理事件 |
| `include_subagent_events` | `True` | 输出已验证子 Agent 事件 |
| `on_native_part` | `None` | 观察每条已验证原生对象 |
| `on_agui_event` | `None` | 每条事件交付前的观察函数 |
| `on_resume_saved` | `None` | marker 持久后的幂等 callback |
| `on_resume_not_saved` | `None` | 只在 marker 持久前执行的幂等结算 |

推理开关不会放行 provider 私有 metadata。

## 高级集成

`DeepAgentDefinition.new_agui()`、`prepare_agui_resume()`、`AgUiResumeBinding` 与
`TinkerFin.failed_agui_run()` 继续服务可信事件日志集成和自定义编排。这些 API 暴露的生命周期
顺序，在普通应用中由 `open_agui_run()` 统一负责。

所选 Runtime Profile 负责必需 mode、上游 version、subgraph 行为与完整 state 输出。通常不需要
填写这些参数；可以增加受支持的诊断 mode，但冲突或不完整的上游参数会在 Graph 副作用前失败。

`parent_run_id` 选择 checkpoint 分支，不表示子 Agent 谱系。父 run 缺失、跨 thread、仍活跃、
已失败、存在歧义、自引用或没有正确恢复时，框架会在 Graph 执行前拒绝。

## 下一步

- [看懂 AG-UI 事件](events.md)
- [interrupt 与恢复](interrupts-and-resume.md)
- [只使用转换器](adapter-extensions.md)
- [AG-UI 使用参考](api-reference.md)
