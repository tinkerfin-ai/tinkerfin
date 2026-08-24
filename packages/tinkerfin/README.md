# tinkerfin

## What it is

`tinkerfin` adds request-scoped native and AG-UI streams to Deep Agents while keeping
the installed `create_deep_agent(...)` and `CompiledStateGraph.astream(...)` parameter
shapes. Streams can be sent directly as SSE or persisted by `tinkerfin-messaging`.

Models, tools, backends, checkpointers, stores, Sandbox resources, and request mapping
remain host-owned.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Redis coordination is optional:

```bash
pip install "tinkerfin[redis]"
```

This extra provides `RedisRunCoordinator` and the reusable auto-renewing
`RedisLeaseLock` with monotonic fencing tokens.

## Quick Start

```python
import asyncio

from tinkerfin import Identity, TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new(identity=Identity(threadId="thread-1", runId="run-1"))
    async for part in runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    ):
        print(part)


asyncio.run(main())
```

`create_deep_agent(...)` records the build call. `new()` creates a fresh Graph and a
single-use Runtime; the Graph iterator starts on first pull.

### Plan Mode

Plan Mode adds a standalone read-only Planning workflow and user review before an
approved task reaches the configured Deep Agent. Enable the capability on an immutable
TinkerFin factory; the installed Deep Agents factory signature does not change.

```python
from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.plan(
    enabled=True,
    default_mode="default",
    planner_model="openai:gpt-5.4",
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
    checkpointer=production_checkpointer,
)

plan_runtime = agent.new(
    identity=Identity(threadId="thread-1", runId="run-1"),
    mode="plan",
)
default_runtime = agent.new_agui(
    identity=Identity(threadId="thread-1", runId="run-2"),
    mode="default",
)
```

Selecting `mode="plan"` requires an explicit Planner model and a concrete
`BaseCheckpointSaver`. TinkerFin never creates an in-process saver or silently weakens
durability. Production hosts must supply a production-grade saver; the same saver,
Store, cache, backend, and runtime context are borrowed by Planning and native
execution. Planning and handoff boundaries use synchronous checkpoint durability, and
an explicit non-`sync` value is rejected.

`mode="default"` calls the unmodified native Deep Agent Graph directly. It does not run
Planning, add middleware, replace the state schema, or create a parent Graph. Native
`write_todos`, Tool/Filesystem review, subagents, cancellation, and error semantics are
therefore identical for ordinary and Plan-capable Definitions.

Only `ls`, `read_file`, `glob`, and `grep` are available to the Planner. Every
clarification question explicitly declares whether it is required. Questions can contain
model-generated single-select options, can optionally allow a free-text answer, and an
optional question can be explicitly skipped. Selecting an option submits only its stable
ID; the Planning workflow derives the trusted label from the checkpointed form. A skipped
answer remains explicit trusted Plan context rather than an omitted payload. Approval freezes a `ConfirmedPlan`
and commits a deterministic handoff bound to the original user message ID. The runtime
then starts the native Deep Agent in the same request; the effective mode becomes
`default` before execution. The public Plan state and value models are available from
`tinkerfin.plan` and are emitted under the root state key `tinkerfin_plan`.

Plan content is independently configurable. Omitting `plan_schema` uses
`StructuredPlanContent`; pass `MarkdownPlanContent` for one exact Markdown document, or
provide a concrete `PlanContentModel` subclass with a stable `schema_id`. The selected
schema is frozen on the returned factory, validated before checkpoint persistence, and
used for Planner output, edits, review, confirmed content, and native handoff.

The default clarification form requires no application models. A host that needs typed,
user-visible metadata can define one concrete Pydantic form and freeze it on the Plan
factory:

```python
from tinkerfin.plan import (
    ClarificationForm,
    ClarificationModel,
    ClarificationOption,
    ClarificationQuestion,
)


class AppQuestionAttributes(ClarificationModel):
    help_text: str


class AppOptionAttributes(ClarificationModel):
    priority: int


class AppOption(ClarificationOption[AppOptionAttributes]):
    pass


class AppQuestion(ClarificationQuestion[AppQuestionAttributes, AppOptionAttributes]):
    options: tuple[AppOption, ...] = ()


class AppClarificationForm(ClarificationForm[AppQuestion]):
    pass


agent = (
    TinkerFin()
    .plan(
        clarification_schema=AppClarificationForm,
    )
    .create_deep_agent(
        model="openai:gpt-5.4",
        checkpointer=production_checkpointer,
    )
)
```

Question and option attributes must inherit `ClarificationModel`; arbitrary dictionary
attributes are rejected. Host models may add discriminant fields, but framework-owned
IDs, display text, answer-path fields, and tuple containers cannot be redefined. The
attributes are model-generated public context whose structure is validated, not
authoritative data for permissions, billing, or compliance. A pending clarification
must resume with the same Definition-bound form schema.

`.plan(...)` returns a separate TinkerFin factory while retaining the configured run
coordinator and global state schema. Every Definition freezes the capability options of
its source factory. `mode="default"` directly selects the native Graph, while
`mode="plan"` selects the standalone Planning Graph. A Plan approval is handed to the
native Graph automatically. The same checkpoint thread can select Plan again on a later
request. An ordinary Definition accepts only `mode="default"`.

Application state shared by all Definitions can be supplied once:

```python
tinkerfin = TinkerFin(state_schema=AppState)
agent = tinkerfin.plan().create_deep_agent(
    model="openai:gpt-5.4",
    state_schema=AgentState,
    checkpointer=production_checkpointer,
)
```

The native Graph still combines only the application, Definition, and upstream
middleware state contracts. The standalone Planning Graph composes the fields it needs
without changing native default topology. A conflicting field fails before a Plan run.
Runtime context continues to use Deep Agents `context_schema`; it is not merged into
state or stored in the checkpoint.

## Core concepts

### Native Runtime

```python
runtime = agent.new(identity=identity, mode="plan", on_part=on_part)

parts = runtime.astream(
    graph_input,
    config,
    context=context,
    stream_mode="values",
)
```

Identity is injected into the Graph config and native output is fixed to v2.
`NativeGraphRunStream` preserves ordering,
backpressure, errors, cancellation, coordination, observer ordering, and cleanup.

### AG-UI Runtime

```python
runtime = agent.new_agui(
    identity=identity,
    mode="default",
    on_part=on_part,
    on_event=on_event,
)

events = runtime.astream(graph_input, config, context=context)
```

When omitted, the AG-UI Runtime fixes `stream_mode` to `messages/tasks/values`,
`version` to `v2`, and `subgraphs` to `True`. Explicit values must match; supported
extra modes are `updates`, `checkpoints`, `debug`, and `custom`. Invalid options fail
before stream side effects.

The returned `AgUiEventStream` uses `RUN_STARTED.input=None`, keeps the
event, interrupt/resume, subagent, reasoning privacy, cancellation, and cleanup
semantics, and can be passed directly to SSE or Messaging. Resumed runs use
`AgUiResumeBinding`; high-level callers do not pass Tool IDs separately.

Plan Runtimes automatically remove their internal top-level state channels at every
public state boundary. Low-level `TinkerFinRun.astream_agui(...)` accepts an explicit
`private_state_keys` frozenset for host-owned channels and preserves nested same-named
business data.

### Ownership

A Definition may create multiple Runtimes; each one owns a separate Graph invocation
and one object stream. Graph construction is synchronous, so async servers should use
their controlled thread boundary around `new()` or `new_agui()` when needed.

A configured coordinator receives the same complete Identity. Coordination does not
replace a LangGraph checkpointer.

### Low-level sources

`TinkerFin.run(...)` handles custom asynchronous sources and low-level integrations.
Deep Agents callers normally use the façade above.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/agui/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
