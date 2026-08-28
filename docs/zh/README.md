# TinkerFin 使用文档

[English](../en/README.md)

TinkerFin 用来运行 Deep Agents、记录语义执行轨迹、把实时过程转换成 AG-UI 事件、可靠地保存和回放事件，以及为 Agent 提供隔离的 Sandbox。

如果你第一次使用，按下面的顺序阅读即可。

| 顺序 | 文档 | 你会学到什么 |
| --- | --- | --- |
| 1 | [Runtime](runtime/index.md) | 创建 Agent，并异步接收运行结果 |
| 2 | [AG-UI](agui/index.md) | 把 Agent 的运行过程发送给聊天界面 |
| 3 | [Tracing](tracing/index.md) | 查询语义消息、执行树、状态和交互 |
| 4 | [Messaging](messaging/index.md) | 保存、回放、续传和取消事件流 |
| 5 | [Sandbox](sandbox/index.md) | 让 Agent 在隔离环境中读写文件和执行命令 |
| 6 | [仓库测试](development/testing.md) | 运行普通测试和临时 Docker 集成测试 |

## 先认识几个词

| 名称 | 简单理解 |
| --- | --- |
| Agent | 调用模型和工具完成任务的程序 |
| Graph | Agent 实际执行的工作流 |
| Runtime | 一次 Graph 运行的控制对象 |
| Plan Mode | 独立完成需求澄清与计划审批，并在批准后交给原生 Deep Agent 执行的工作流 |
| stream | 运行过程中连续产生的数据 |
| RunIdentity | 只包含稳定 `threadId` 与一次运行 `runId` 的框架身份 |
| thread | 一段可继续的会话，对应 `RunIdentity.threadId` |
| run | thread 中的一次语义执行，对应 `RunIdentity.runId` |
| Trace | 由 Runtime 生命周期和校验后的 Native 事实生成的用户可读语义历史 |
| AG-UI | 前端和 Agent 交换运行事件的协议 |
| SSE | 服务端持续向浏览器发送事件的一种 HTTP 格式 |
| Messaging | 保存并投递事件流的组件 |
| Sandbox | Agent 可执行命令、操作文件的隔离环境 |

## 安装选择

要求 Python 3.11 或更高版本。

| 需要的能力 | 安装命令 |
| --- | --- |
| 原生 Runtime、Plan Mode 与 Observation | `pip install tinkerfin` |
| 示例使用的 OpenAI 模型适配包 | `pip install langchain-openai` |
| AG-UI Runtime | `pip install "tinkerfin[agui]"` |
| 只使用 AG-UI 转换器 | `pip install tinkerfin-agui-adapter` |
| 共享运行与观察契约 | `pip install tinkerfin-contracts` |
| 共享 Native 流契约 | `pip install tinkerfin-native-stream` |
| 进程内语义 Trace | `pip install tinkerfin-tracing` |
| SQLite Trace 持久化 | `pip install "tinkerfin-tracing[sqlite]"` |
| MySQL Trace 持久化 | `pip install "tinkerfin-tracing[mysql]"` |
| asyncmy LangGraph MySQL Store | `pip install tinkerfin-langgraph-mysql` |
| 与协议无关的内存消息流 | `pip install tinkerfin-messaging` |
| AG-UI Messaging codec | `pip install "tinkerfin-messaging[agui]"` |
| Native Messaging codec | `pip install "tinkerfin-messaging[native]"` |
| Redis AG-UI 消息存储 | `pip install "tinkerfin-messaging[agui,redis]"` |
| OpenSandbox | `pip install tinkerfin-sandbox` |
| SQLite 持久化 Sandbox 状态 | `pip install "tinkerfin-sandbox[sqlite]"` |
| MySQL 持久化 Sandbox 状态 | `pip install "tinkerfin-sandbox[mysql]"` |

不必一次安装全部组件。先安装当前要用的部分，后面需要时再增加。
