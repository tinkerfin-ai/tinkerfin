# Installation and optional components

[Documentation](index.md) · [中文](../cn/installation.md)


Python 3.11 or newer is required.

| Capability | Command |
| --- | --- |
| Native Runtime, Plan Mode, and Observation | `pip install tinkerfin` |
| OpenAI model adapter used by examples | `pip install langchain-openai` |
| AG-UI Runtime | `pip install "tinkerfin[agui]"` |
| AG-UI conversion only | `pip install tinkerfin-agui-adapter` |
| Shared run and observation contracts | `pip install tinkerfin-contracts` |
| Shared Native stream contract | `pip install tinkerfin-native-stream` |
| In-memory semantic tracing | `pip install tinkerfin-tracing` |
| SQLite Trace persistence | `pip install "tinkerfin-tracing[sqlite]"` |
| MySQL Trace persistence | `pip install "tinkerfin-tracing[mysql]"` |
| Asyncmy LangGraph MySQL Store | `pip install tinkerfin-langgraph-mysql` |
| Protocol-neutral in-memory messaging | `pip install tinkerfin-messaging` |
| AG-UI messaging codec | `pip install "tinkerfin-messaging[agui]"` |
| Native messaging codec | `pip install "tinkerfin-messaging[native]"` |
| Redis AG-UI message storage | `pip install "tinkerfin-messaging[agui,redis]"` |
| OpenSandbox | `pip install tinkerfin-sandbox` |
| SQLite Sandbox state | `pip install "tinkerfin-sandbox[sqlite]"` |
| MySQL Sandbox state | `pip install "tinkerfin-sandbox[mysql]"` |

You do not need every package at once. Start with the capability you need now.
