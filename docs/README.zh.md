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
docs/                          中文与英文使用文档
```

## 安装

要求 Python 3.11 或更高版本。

```bash
pip install tinkerfin
```

按需安装集成：

```bash
pip install "tinkerfin[redis]"
pip install "tinkerfin-messaging[redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## 快速开始

```python
import asyncio

from tinkerfin import Identity, TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = Identity(threadId="thread-1", runId="run-1")
    runtime = agent.new_agui(identity=identity)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
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
- `TinkerFin(state_schema=...)` 为该 factory 创建的所有 Deep Agent Definition 提供
  应用级 state；Definition state 与 middleware state 会在不破坏 reducer 和必填语义的
  前提下自动组合
- `.plan(enabled=True)` 在不改变 Deep Agents `create_deep_agent(...)` 参数的前提下
  创建稳定父工作流；每次 `new()` / `new_agui()` 通过 `mode="default"` 或
  `mode="plan"` 选择本轮路径；选择 Plan 时必须提供具体 checkpointer
- AG-UI 固定使用 v2 `messages`、`tasks`、`values` 和 `subgraphs=True`，非法参数会在
  迭代及生命周期事件开始前失败
- Runtime 与 Adapter 负责保证事件顺序、子 Agent 来源、interrupt/resume、推理隐私、
  取消、背压和清理语义
- 对象流可以直接输出 SSE，也可以交给 Messaging 持久化、回放、附着和远程取消
- AG-UI Runtime 使用一个 `Identity` 统一公开事件、Graph、checkpoint 与持久投递；可选
  `parent_run_id` 会创建真实 checkpoint 分支
- 恢复请求通过 `AgUiResumeBinding.from_agui(...)` 构造；Runtime 自己管理原生 Command、
  Tool 关联、取消与持久 checkpoint 证据

## 文档

- [完整使用文档](zh/README.md)
- [Runtime](zh/runtime/index.md)
- [AG-UI](zh/agui/index.md)
- [Messaging](zh/messaging/index.md)
- [Sandbox](zh/sandbox/index.md)
- [仓库测试](zh/development/testing.md)
- [核心包](../packages/tinkerfin/README.md)
- [AG-UI adapter](../packages/tinkerfin-agui-adapter/README.md)
- [Studio 服务端](../apps/studio/server/README.md)
- [Studio Web 客户端](../apps/studio/web/README.md)

## 许可证

Apache License 2.0，详见 [LICENSE](../LICENSE)。
