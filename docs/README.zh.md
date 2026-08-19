# TinkerFin

[English](../README.md)

## 项目简介

TinkerFin 为 Deep Agents 提供原生流、AG-UI、SSE 和可持久化的 Messaging 交付。
模型、工具、backend、checkpoint、store 和 Sandbox 资源仍由宿主管理。

```text
packages/
├── tinkerfin/                 Deep Agents Runtime、AG-UI、SSE 与运行协调
├── tinkerfin-agui-adapter/    LangGraph v2 StreamPart 到 AG-UI 的转换
├── tinkerfin-messaging/       持久交付、回放、取消与 Redis
└── tinkerfin-sandbox/         OpenSandbox backend 与生命周期管理
apps/studio/
├── server/                    Studio 服务端
└── web/                       Studio Web 应用
```

## 安装

要求 Python 3.11 或更高版本。

```bash
pip install tinkerfin
```

按需安装集成：

```bash
pip install "tinkerfin[redis]"
pip install "tinkerfin-messaging[agui,native,redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## 快速开始

```python
import asyncio

from ag_ui.core import RunAgentInput
from tinkerfin import TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    run_input = RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    runtime = agent.new_agui(run_input=run_input)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
        {"configurable": {"thread_id": "thread-1"}},
    )
    async for event in events:
        print(event)


asyncio.run(main())
```

需要 LangGraph 原生对象时使用 `agent.new()`。两种 Runtime 的 `astream(...)` 都保留
当前 `CompiledStateGraph.astream(...)` 的参数形状。

## 核心概念

- `create_deep_agent(...)` 记录 Deep Agents 建图参数，`new()` / `new_agui()` 创建新
  Graph 和单次使用的 Runtime
- AG-UI 固定使用 v2 `messages`、`tasks`、`values` 和 `subgraphs=True`，非法参数会在
  迭代及生命周期事件开始前失败
- 现有事件顺序、子 Agent 来源、interrupt/resume、推理隐私、取消、背压和清理语义
  保持不变
- 对象流可以直接输出 SSE，也可以交给 Messaging 持久化、回放、附着和远程取消
- `RUN_STARTED.input` 携带调用方完整的 `RunAgentInput`；恢复请求通过
  `AgUiResumeBinding` 绑定该输入、原生 Command 和完整 Tool ID
- `TinkerFin.run(...)` 继续用于自定义异步源

## 文档

- [完整使用文档](zh/README.md)
- [Runtime](zh/runtime/index.md)
- [AG-UI](zh/agui/index.md)
- [Messaging](zh/messaging/index.md)
- [Sandbox](zh/sandbox/index.md)
- [核心包](../packages/tinkerfin/README.md)
- [AG-UI adapter](../packages/tinkerfin-agui-adapter/README.md)
- [Studio 服务端](../apps/studio/server/README.md)
- [Studio Web 客户端](../apps/studio/web/README.md)

## 许可证

Apache License 2.0，详见 [LICENSE](../LICENSE)。
