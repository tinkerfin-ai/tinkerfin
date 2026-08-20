# TinkerFin documentation

[中文](../zh/README.md)

TinkerFin runs Deep Agents, converts live runs into AG-UI events, stores and replays event streams, and gives agents isolated Sandbox resources.

If this is your first time using TinkerFin, read these sections in order.

| Order | Guide | What you will learn |
| --- | --- | --- |
| 1 | [Runtime](runtime/index.md) | Create an agent and consume a run asynchronously |
| 2 | [AG-UI](agui/index.md) | Send an agent run to a chat interface |
| 3 | [Messaging](messaging/index.md) | Persist, replay, resume, and cancel streams |
| 4 | [Sandbox](sandbox/index.md) | Let an agent work with files and commands in isolation |

## A few terms first

| Term | Plain meaning |
| --- | --- |
| Agent | A program that uses a model and tools to complete a task |
| Graph | The workflow that actually executes the agent |
| Runtime | The control object for one Graph run |
| stream | Data produced continuously while a run is active |
| Identity | Framework identity containing only a stable `threadId` and one `runId` |
| thread | A continuing conversation identified by `Identity.threadId` |
| run | One semantic execution identified by `Identity.runId` |
| AG-UI | A protocol for exchanging live agent events with a frontend |
| SSE | An HTTP format for sending a continuing event stream to a browser |
| Messaging | The component that persists and delivers streams |
| Sandbox | An isolated environment where an agent can use files and commands |

## Choose what to install

Python 3.11 or newer is required.

| Capability | Command |
| --- | --- |
| Runtime and AG-UI Runtime | `pip install tinkerfin` |
| AG-UI conversion only | `pip install tinkerfin-agui-adapter` |
| In-memory messaging, AG-UI, and Native codecs | `pip install tinkerfin-messaging` |
| Redis message storage | `pip install "tinkerfin-messaging[redis]"` |
| OpenSandbox | `pip install tinkerfin-sandbox` |
| SQLite Sandbox state | `pip install "tinkerfin-sandbox[sqlite]"` |
| MySQL Sandbox state | `pip install "tinkerfin-sandbox[mysql]"` |

You do not need every package at once. Start with the capability you need now.
