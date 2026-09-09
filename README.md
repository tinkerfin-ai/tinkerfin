<p align="center">
  <img src="apps/studio/web/public/brand/tinkerfin-mark.png" alt="TinkerFin" width="88" />
</p>
<h1 align="center">TinkerFin</h1>
<p align="center"><strong>Build and deliver enterprise agent applications, faster.</strong></p>
<p align="center">
  <a href="README.md">English</a> · <a href="README.cn.md">简体中文</a> ·
  <a href="docs/en/index.md">Documentation</a> · <a href="docs/en/quick_start.md">Quick Start</a>
</p>
<p align="center">
  <a href="docs/en/runtime/quick_start.md"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" /></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-52617A?style=flat-square" /></a>
  <a href="https://github.com/tinkerfin-ai/tinkerfin/actions/workflows/packages-quality.yml"><img alt="Package checks" src="https://github.com/tinkerfin-ai/tinkerfin/actions/workflows/packages-quality.yml/badge.svg" /></a>
  <a href="docs/en/index.md"><img alt="Docs: English / 中文" src="https://img.shields.io/badge/Docs-English%20%2F%20中文-2563EB?style=flat-square" /></a>
</p>

**TinkerFin is an agent application framework built for enterprise workflows.** It combines task orchestration, human collaboration, execution tracing, and isolated environments to turn business workflows into applications that **plan, act, and keep people in control**.

Connect model capabilities to your business—from research and document workflows to data processing and file generation.
**Build the application core with Python and deliver the workspace with Studio.** Cover model and tool integration, user interactions, execution management, and results with less application infrastructure to build from scratch.

![Studio demo with a conversation, task checklist, and report](docs/assets/screenshots/studio-en.png)

## Framework capabilities

- **Compose the capabilities your application needs** — Configure models, tools, skills, and subagents, then compose run management, messaging, tracing, and sandbox capabilities as needed. Connect your own frontend or use Studio.
- **Review the plan before execution** — Plan mode clarifies requirements and drafts a plan for user approval before handing it to the agent. Selected tool operations can require separate approval, keeping people involved in decisions throughout a task.
- **Stream events and replay after reconnection** — Convert agent output to AG-UI events, then persist, deliver, and replay them through a message channel. Application code can publish custom events during a run through the same subscription and replay mechanism as agent output.
- **Follow a conversation down to each call** — Trace relationships, inputs, outputs, and timing across models, tools, and subagents. Follow progress live or query past executions to find failed steps and slow operations.
- **Manage dedicated agent workspaces** — Read files, write files, and run commands in isolated environments, with reuse, warm capacity, pause, and resume. Your application decides how environments are assigned; the framework manages connections and resource lifecycles.
- **Persist state and coordinate across processes** — Connect checkpointers, storage, and run coordination as needed to retain conversation state and continue after approval. Manage run ownership, duplicate requests, and cancellation across service processes.
- **Multitenant integration** — Let your application scope conversations and sandboxes by tenant, user, or project, and separate data through storage namespaces. Your application remains responsible for authentication and access authorization.
- **Context management (planned)** — Select relevant material, compress lengthy histories, and retain goals, constraints, and intermediate findings so limited context stays focused on what matters for the next decision.
- **Long-term memory governance (planned)** — Store and retrieve user preferences, project knowledge, and lessons from tasks across conversations, with source tracking, tenant isolation, updates and corrections, expiration policies, and explicit deletion.
- **Task automation (planned)** — Scheduled and event-triggered runs are planned so recurring work and business events can start agent tasks without a new manual conversation each time.

## Studio: a multimodal agent workspace

Start tasks with text, images, and documents. Analyze content, generate images, and receive files in the same conversation. Image understanding and generation require the corresponding models to be configured.

### Turn business goals into execution plans

Turn a business goal into a reviewable plan, then confirm the scope and steps before execution.

![Studio plan review demo](docs/assets/screenshots/plan-en.png)

### Understand how each task runs

Follow a business conversation through model calls, tools, and subagents to see how the task ran and where time was spent.

![Studio execution trace demo](docs/assets/screenshots/trace-en.png)

*Screenshots show the current Studio interface with demonstration data.*

## Quick Start

### Try Studio

Prepare Docker and Docker Compose, then start the backend and Web client. Sign in with the initial username `tinkerfin` and password `123456`, then configure your model.

[Open the Studio setup guide →](docs/en/studio/quick_start.md)

### Build with Python

Requires Python 3.11 or newer.

```bash
pip install tinkerfin langchain-openai
```

Set your model credentials, create an agent, and consume its output.

[Run your first agent →](docs/en/runtime/quick_start.md)

## Project layout

| Part | Purpose |
| --- | --- |
| `packages/` | Core framework for enterprise agent applications: task orchestration, human collaboration, execution tracing, and isolated environments |
| `apps/studio/` | A deployable, extensible agent workspace for business interactions, plan approval, and execution tracing |
| `docs/` | Bilingual documentation for application development, deployment, operations, and advanced integration |

## Documentation

[English documentation →](docs/en/index.md)

From application development to deployment and operations: getting-started guides, architecture documentation, and integration references.

## Contributing

Issue reports, documentation improvements, and code contributions are welcome. See the [development guide](docs/en/development.md) for environment setup and validation commands.

- [Contributing](CONTRIBUTING.md)
- [Security reports](SECURITY.md)

## License

[Apache License 2.0](LICENSE) is the repository default. The `tinkerfin-langgraph-mysql` package,
derived from the upstream LangGraph MySQL Store, uses its packaged
[MIT License](packages/tinkerfin-langgraph-mysql/LICENSE) and [NOTICE](packages/tinkerfin-langgraph-mysql/NOTICE).
