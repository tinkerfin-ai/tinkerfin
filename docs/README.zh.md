# TinkerFin

[English](../README.md)

## 项目简介

TinkerFin 为 Deep Agents 提供原生流、AG-UI、SSE、语义 Trace 和可持久化的 Messaging 交付。
模型、工具、backend、checkpoint、store 和 Sandbox 资源仍由宿主管理。

```text
packages/
├── tinkerfin-contracts/       与协议无关的运行身份和观察契约
├── tinkerfin-native-stream/   当前 Deep Agents/LangGraph 原生流契约
├── tinkerfin/                 Deep Agents Runtime、可选 AG-UI、SSE 与运行协调
├── tinkerfin-agui-adapter/    LangGraph v2 StreamPart 到 AG-UI 的转换
├── tinkerfin-tracing/         语义 Ledger、查询与 Projection
├── tinkerfin-langgraph-mysql/ 仅 asyncmy 的 LangGraph MySQL Store
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

快速开始中的 OpenAI 模型字符串需要 LangChain 的对应适配包：

```bash
pip install langchain-openai
```

按需安装集成：

```bash
pip install "tinkerfin[agui]"
pip install "tinkerfin[redis]"
pip install tinkerfin-tracing
pip install "tinkerfin-tracing[mysql]"
pip install tinkerfin-langgraph-mysql
pip install "tinkerfin-messaging[agui,redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## 快速开始

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = RunIdentity(threadId="thread-1", runId="run-1")
    runtime = agent.new(identity=identity)
    parts = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for part in parts:
        print(part)


asyncio.run(main())
```

需要 LangGraph 原生对象时使用 `agent.new()`。两种 Runtime 的 `astream(...)` 都保留
当前 `CompiledStateGraph.astream(...)` 的参数形状，并固定为完整 Native profile。

## 核心概念

- `create_deep_agent(...)` 记录 Deep Agents 建图参数，`new()` / `new_agui()` 创建新
  Graph 和单次使用的 Runtime
- 原生 Runtime、Plan Mode、Observation 与 SSE 默认可用；`new_agui()` 需要安装
  `tinkerfin[agui]`
- `TinkerFin(state_schema=...)` 为该 factory 创建的所有 Deep Agent Definition 提供
  应用级 state；Definition state 与 middleware state 会在不破坏 reducer 和必填语义的
  前提下自动组合
- `.plan(enabled=True)` 在不改变 Deep Agents `create_deep_agent(...)` 参数的前提下
  创建稳定父工作流；每次 `new()` / `new_agui()` 通过 `mode="default"` 或
  `mode="plan"` 选择本轮路径；选择 Plan 时必须提供具体 checkpointer
- 一个显式 Runtime Profile 完整负责建图、必需流参数、Native 校验、Observation 与 canonical
  replay；冲突或局部上游参数会在迭代及生命周期事件开始前失败
- Runtime 与 Adapter 负责保证事件顺序、子 Agent 来源、interrupt/resume、推理隐私、
  取消、背压和清理语义
- 对象流可以直接输出 SSE，也可以交给 Messaging 持久化、回放、附着和远程取消
- AG-UI Runtime 使用一个 `RunIdentity` 统一公开事件、Graph、checkpoint 与持久投递；可选
  `parent_run_id` 会创建真实 checkpoint 分支
- 恢复请求使用 `AgUiResumeRequest`；`DeepAgentDefinition.prepare_agui_resume()` 从权威
  checkpoint 生成私有 binding，Runtime 自己管理原生 Command、Tool 关联、取消与持久证据
- `.observe(Tracer())` 以 fail-closed 方式记录 Runtime 生命周期与校验后的 Native 语义，
  不记录 AG-UI、Messaging、SSE 或 Redis 投递状态
- 当前发行只提供显式的 `deepagents-v2` Runtime Profile，不提供 Deep Agents v3 Profile 或
  TodoGroups 投影与界面；新的真实 Profile 必须输出同一 canonical 合同，不能在下游增加版本分支
- 当前不提供 Archive/S3/Blob、payload Encryption/KMS 或 OpenTelemetry exporter；未来存储与
  可观测集成只能组合已真实使用的 `TraceStore`、canonical codec、`RuntimeObserver` 或
  Store/Messaging Backend decorator，不能增加占位 API

## 文档

- [完整使用文档](zh/README.md)
- [Runtime](zh/runtime/index.md)
- [AG-UI](zh/agui/index.md)
- [Messaging](zh/messaging/index.md)
- [Tracing](zh/tracing/index.md)
- [Sandbox](zh/sandbox/index.md)
- [仓库测试](zh/development/testing.md)
- [核心包](../packages/tinkerfin/README.md)
- [共享 contracts](../packages/tinkerfin-contracts/README.md)
- [AG-UI adapter](../packages/tinkerfin-agui-adapter/README.md)
- [Tracing 包](../packages/tinkerfin-tracing/README.md)
- [LangGraph MySQL Store](../packages/tinkerfin-langgraph-mysql/README.md)
- [Studio 服务端](../apps/studio/server/README.md)
- [Studio Web 客户端](../apps/studio/web/README.md)

## 许可证

仓库默认采用 Apache License 2.0，详见 [LICENSE](../LICENSE)。源自上游 LangGraph MySQL Store 的
`tinkerfin-langgraph-mysql` 采用包内 [MIT LICENSE](../packages/tinkerfin-langgraph-mysql/LICENSE)
与 [NOTICE](../packages/tinkerfin-langgraph-mysql/NOTICE)。
