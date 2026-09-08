# 安装与可选组件

[Documentation](index.md) · [English](../en/installation.md)


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
