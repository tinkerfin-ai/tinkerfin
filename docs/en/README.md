# TinkerFin documentation

[中文](../zh/README.md)

TinkerFin runs Deep Agents, records semantic execution traces, converts live runs into AG-UI events, stores and replays event streams, and gives agents isolated Sandbox resources.

If this is your first time using TinkerFin, read these sections in order.

| Order | Guide | What you will learn |
| --- | --- | --- |
| 1 | [Runtime](runtime/index.md) | Create an agent and consume a run asynchronously |
| 2 | [AG-UI](agui/index.md) | Send an agent run to a chat interface |
| 3 | [Tracing](tracing/index.md) | Query semantic messages, execution trees, state, and interactions |
| 4 | [Messaging](messaging/index.md) | Persist, replay, resume, and cancel streams |
| 5 | [Sandbox](sandbox/index.md) | Let an agent work with files and commands in isolation |

## A few terms first

| Term | Plain meaning |
| --- | --- |
| Agent | A program that uses a model and tools to complete a task |
| Graph | The workflow that actually executes the agent |
| Runtime | The control object for one Graph run |
| Plan Mode | A standalone workflow that clarifies and reviews a Plan, then hands approval to native Deep Agent execution |
| stream | Data produced continuously while a run is active |
| RunIdentity | Framework identity containing only a stable `threadId` and one `runId` |
| thread | A continuing conversation identified by `RunIdentity.threadId` |
| run | One semantic execution identified by `RunIdentity.runId` |
| Trace | User-visible semantic history derived from Runtime lifecycle and validated Native facts |
| AG-UI | A protocol for exchanging live agent events with a frontend |
| SSE | An HTTP format for sending a continuing event stream to a browser |
| Messaging | The component that persists and delivers streams |
| Sandbox | An isolated environment where an agent can use files and commands |

## Choose what to install

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

## Repository development

[Build working-tree wheels and validate packaging](development.md).
