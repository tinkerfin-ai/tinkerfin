# TinkerFin 使用文档

[English](../en/README.md)

TinkerFin 用来运行 Deep Agents、把运行过程转换成 AG-UI 事件、可靠地保存和回放事件，以及为 Agent 提供隔离的 Sandbox。

如果你第一次使用，按下面的顺序阅读即可。

| 顺序 | 文档 | 你会学到什么 |
| --- | --- | --- |
| 1 | [Runtime](runtime/index.md) | 创建 Agent，并异步接收运行结果 |
| 2 | [AG-UI](agui/index.md) | 把 Agent 的运行过程发送给聊天界面 |
| 3 | [Messaging](messaging/index.md) | 保存、回放、续传和取消事件流 |
| 4 | [Sandbox](sandbox/index.md) | 让 Agent 在隔离环境中读写文件和执行命令 |

## 先认识几个词

| 名称 | 简单理解 |
| --- | --- |
| Agent | 调用模型和工具完成任务的程序 |
| Graph | Agent 实际执行的工作流 |
| Runtime | 一次 Graph 运行的控制对象 |
| stream | 运行过程中连续产生的数据 |
| Identity | 只包含稳定 `threadId` 与一次运行 `runId` 的框架身份 |
| thread | 一段可继续的会话，对应 `Identity.threadId` |
| run | thread 中的一次语义执行，对应 `Identity.runId` |
| AG-UI | 前端和 Agent 交换运行事件的协议 |
| SSE | 服务端持续向浏览器发送事件的一种 HTTP 格式 |
| Messaging | 保存并投递事件流的组件 |
| Sandbox | Agent 可执行命令、操作文件的隔离环境 |

## 安装选择

要求 Python 3.11 或更高版本。

| 需要的能力 | 安装命令 |
| --- | --- |
| Runtime 和 AG-UI Runtime | `pip install tinkerfin` |
| 只使用 AG-UI 转换器 | `pip install tinkerfin-agui-adapter` |
| 内存消息流、AG-UI 与 Native codec | `pip install tinkerfin-messaging` |
| Redis 消息存储 | `pip install "tinkerfin-messaging[redis]"` |
| OpenSandbox | `pip install tinkerfin-sandbox` |
| SQLite 持久化 Sandbox 状态 | `pip install "tinkerfin-sandbox[sqlite]"` |
| MySQL 持久化 Sandbox 状态 | `pip install "tinkerfin-sandbox[mysql]"` |

不必一次安装全部组件。先安装当前要用的部分，后面需要时再增加。
