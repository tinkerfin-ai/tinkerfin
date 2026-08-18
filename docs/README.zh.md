# TinkerFin

[English](../README.md)

## 项目简介

TinkerFin 把调用方拥有的 LangGraph v2 异步源绑定为原生流、AG-UI、直接 SSE
或可持久化的 Messaging 交付。Graph 构造与调用参数、模型、工具、中间件、
checkpoint、store 和 Sandbox 生命周期均由宿主负责。

```text
packages/
├── tinkerfin/                 流源绑定、AG-UI、直接 SSE 与运行协调
├── tinkerfin-agui-adapter/    LangGraph v2 StreamPart 到 AG-UI 的转换
├── tinkerfin-messaging/       持久交付、回放、取消与 Redis
└── tinkerfin-sandbox/         OpenSandbox backend 与生命周期管理
apps/studio/                   Studio 服务端与 Web 应用
docs/                          运行时与集成文档
```

## 安装

要求 Python 3.11 或更高版本。

```bash
pip install tinkerfin
```

应用只安装实际需要的集成：

```bash
pip install "tinkerfin[redis]"
pip install "tinkerfin-messaging[agui,native,redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## 快速开始

惰性绑定由调用方拥有的 Graph 流，再通过同一个运行接口选择原生对象或 AG-UI
对象。

```python
import asyncio
from typing import TypedDict

from langgraph.graph import START, StateGraph
from tinkerfin import TinkerFin


class State(TypedDict):
    message: str


async def echo(state: State) -> dict[str, str]:
    return {"message": f"Echo: {state['message']}"}


builder = StateGraph(State)
builder.add_node("echo", echo)
builder.add_edge(START, "echo")
graph = builder.compile()
tinkerfin = TinkerFin()


async def main() -> None:
    run = tinkerfin.run(
        lambda: graph.astream(
            {"message": "Hello"},
            config={"configurable": {"thread_id": "thread-1"}},
            stream_mode=("messages", "tasks", "values"),
            version="v2",
            subgraphs=True,
        )
    )
    events = run.astream_agui(thread_id="thread-1", run_id="run-1")
    async for event in events:
        print(event)


asyncio.run(main())
```

原生对象使用 `run.astream()`。直接 HTTP SSE 使用 `events.to_sse()` 或
`run.astream().to_sse()`。需要回放时，把尚未编码的对象流交给可复用的
Messaging name-only channel。

## 核心概念

- `TinkerFin` 是应用级无状态对象；`TinkerFin.run(source_factory,
  principal=None, on_part=None)` 惰性绑定一条对象源
- 一个 `TinkerFinRun` 只能通过 `astream()` 或 `astream_agui(...)` 取得一条对象流
- AG-UI 转换消费启用 `subgraphs=True` 的 v2 `messages`、`tasks`、`values`，并校验
  每个实际到达的 part
- `AgUiNativeStreamConfig(extra_modes=...).bind(...)` 提供可选的严格 source，由同一个
  `run()` 固定并预检这些 Graph 参数
- 普通 compiled subgraph 与 Deep Agents `task` 子代理是两种不同来源；namespace
  provenance 不会改变 AG-UI `parentRunId`
- 直接 `to_sse()` 支持自定义 payload mapper 与事件 ID；Messaging 拒绝预编码 SSE
- `messaging.channel(name=...)` 从严格原生流推断原生 codec，从 `AgUiEventStream`
  推断 AG-UI codec，且不读取第一个事件；channel handle 可复用，每条 source 只使用一次
- 宿主负责把 `RunAgentInput` 与 checkpoint 状态映射为 graph input 或
  `Command(resume=...)`，TinkerFin 不伪造协议输入

## 文档

- [运行时与集成指南](runtime.md)
- [核心包](../packages/tinkerfin/README.md)
- [AG-UI adapter](../packages/tinkerfin-agui-adapter/README.md)
- [Messaging](../packages/tinkerfin-messaging/README.md)
- [OpenSandbox 集成](../packages/tinkerfin-sandbox/README.md)
- [Studio 服务端](../apps/studio/server/README.md)
- [Studio Web 客户端](../apps/studio/web/README.md)

## 许可证

Apache License 2.0，详见 [LICENSE](../LICENSE)。
