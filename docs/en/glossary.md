# Glossary

[Documentation](index.md) · [中文](../cn/glossary.md)


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
