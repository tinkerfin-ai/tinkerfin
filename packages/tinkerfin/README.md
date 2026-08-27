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

Plan review defaults to `PlanReviewAction.APPROVE`, `RESPOND`, and `REJECT`.
`.plan(allowed_review_actions=...)` accepts an ordered, non-empty sequence of distinct
`PlanReviewAction` values and publishes exactly those decisions in the interrupt
response Schema. A host with a trusted draft editor can explicitly include
`PlanReviewAction.EDIT`; editing is not enabled by default.

Selecting `mode="plan"` requires an explicit Planner model and a concrete
`BaseCheckpointSaver`. TinkerFin never creates an in-process saver or silently weakens
durability. Production hosts must supply a production-grade saver; the same saver,
Store, cache, backend, and runtime context are borrowed by Planning and native
execution. Planning and handoff boundaries use synchronous checkpoint durability, and
an explicit non-`sync` value is rejected.

`mode="default"` calls the native Deep Agent Graph directly without running Planning or
creating a parent Graph. Every Definition includes a private resume marker channel.
Definitions with Tool review also replace the existing Deep Agents patch middleware slot
with a locked-dependency adapter that preserves patch behavior and delegates
approve/edit/reject/respond to LangChain unchanged. Its only extension is an internal
cancel decision used for mixed AG-UI resume batches.

Only `ls`, `read_file`, `glob`, and `grep` are available to the Planner. Every
clarification question explicitly declares whether it is required and selects one semantic
answer type: `single_choice`, `multiple_choice`, `text`, or `date`. Choice questions can
accept one custom alternative, and optional questions can be explicitly skipped. Clients
submit stable option IDs or typed values under the checkpoint question ID; Planning derives
trusted option labels and canonical values from the checkpointed form. A skipped answer
remains explicit trusted Plan context rather than an omitted payload. Approval freezes a `ConfirmedPlan`
and commits a deterministic handoff bound to the original user message ID. The runtime
then starts the native Deep Agent in the same request; the effective mode becomes
`default` before execution. The public Plan state and value models are available from
`tinkerfin.plan` and are emitted under the root state key `tinkerfin_plan`.

Plan content is independently configurable. Omitting `content_schema` uses
`StructuredPlanContent`; pass `MarkdownPlanContent` for one exact Markdown document, or
provide a concrete `PlanContentModel` subclass. The selected schema is frozen on the
returned factory, fingerprinted automatically, validated before checkpoint persistence,
and used for Planner output, any explicitly enabled edit, review, confirmed content, and
native handoff.

The default clarification form requires no application models. A host that needs typed,
user-visible metadata can define one concrete Pydantic form and freeze it on the Plan
factory:

```python
from tinkerfin.plan import (
    BuiltInClarificationForm,
    ClarificationModel,
    ClarificationOptionBase,
)


class AppQuestionAttributes(ClarificationModel):
    help_text: str


class AppOptionAttributes(ClarificationModel):
    priority: int


class AppOption(ClarificationOptionBase):
    attributes: AppOptionAttributes


class AppClarificationForm(BuiltInClarificationForm[AppQuestionAttributes, AppOption]):
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
attributes are rejected. Inherit `ClarificationOptionBase` and declare `attributes` as
shown when metadata is required, or use `ClarificationOption[AppOptionAttributes]` when
it is optional. The configured Form union can include only a subset of the built-in
question types. The attributes are model-generated public context whose structure is
validated, not authoritative data for permissions, billing, or compliance. A pending
clarification resumes only after its exact Form and response Schema pass the
Definition-bound preflight.

A host-defined semantic answer type is one immutable registration unit:

```python
from typing import Literal

from pydantic import Field
from tinkerfin.plan import (
    ClarificationQuestionBase,
    ClarificationResponseBase,
    clarification_type,
)


class RatingQuestion(ClarificationQuestionBase):
    answer_type: Literal["acme:rating.v1"] = "acme:rating.v1"


class RatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating.v1"] = "acme:rating.v1"
    rating: int = Field(ge=1, le=5)


rating_type = clarification_type(
    type_id="acme:rating.v1",
    description="Use for one bounded integer rating.",
    question_model=RatingQuestion,
    response_model=RatingResponse,
    normalize=lambda _question, response: {"rating": response.rating},
)

agent = (
    TinkerFin()
    .plan(clarification_types=(rating_type,))
    .create_deep_agent(
        model="openai:gpt-5.4",
        checkpointer=production_checkpointer,
    )
)
```

Custom type IDs are versioned and namespaced. Their callbacks are synchronous,
deterministic, and free of external I/O. The host client supplies a renderer for each
custom type it enables. Changing a per-question Schema, validator, or normalizer requires
a new type ID version so an existing checkpoint cannot acquire different semantics.

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

The native Graph combines the private resume marker, application, Definition, and
upstream middleware state contracts. The standalone Planning Graph composes the fields
it needs without changing native default topology. A conflicting field fails before a
Plan run.
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
from tinkerfin import AgUiResumeBinding, Identity


runtime = agent.new_agui(
    identity=Identity(threadId="thread-1", runId="run-1"),
    parent_run_id=parent_run_id,
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

`Identity` is the one canonical identity used by AG-UI lifecycle events, the Graph,
checkpoints, coordination, and Messaging. `RUN_STARTED.input` is omitted because Graph
input is already explicit. A non-empty `parent_run_id` resolves the unique completed
checkpoint leaf for that run in the same thread, so A→B followed by a request with parent
A creates A→C. Missing, cross-thread, active, failed, ambiguous, or improperly resumed
parents fail before Graph execution.

The returned `AgUiEventStream` preserves event, interrupt/resume, subagent, reasoning
privacy, cancellation, and cleanup semantics and can be passed directly to SSE or
Messaging. Build resumed runs once from trusted, server-persisted AG-UI facts:

```python
binding = AgUiResumeBinding.from_agui(
    entries=resume_entries,
    interrupts=persisted_interrupts,
)
runtime = agent.new_agui(
    identity=Identity(threadId="thread-1", runId="run-resume"),
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
)
events = runtime.astream(config=config, context=context)
```

The binding owns validated native resume, Tool correlation, cancellation, and provenance
facts and has a stable JSON round trip. It owns no identity or parent and exposes no
native `Command`. The framework atomically checkpoints a private marker with the native
decision. `on_resume_checkpointed` runs only after that marker is readable and may receive
the same `AgUiResumeCheckpoint` again on retry, so hosts must consume it idempotently.
The retry continuation remains internal and never resubmits the decision. An entirely
cancelled binding emits a finite cancelled lifecycle without creating or invoking a Graph.

AG-UI batches may mix resolved and cancelled Tool reviews. Resolved Tools retain native
behavior; cancelled Tools are removed from execution and receive a deterministic error
`ToolMessage` with `outcome="cancelled"` and `executed=False`. Main, automatic
general-purpose, declarative subagents, and permission-generated reviews use this
contract automatically. An externally compiled or remote subagent that can emit these
reviews must declare
`tinkerfin_hitl_contract=TINKERFIN_HITL_CONTRACT` in its subagent spec.

Plan Runtimes automatically remove their internal top-level state channels at every
public state boundary while preserving nested same-named business data.

### Ownership

A Definition may create multiple Runtimes; each one owns a separate Graph invocation
and one object stream. Graph construction is synchronous, so async servers should use
their controlled thread boundary around `new()` or `new_agui()` when needed.

A configured coordinator receives the same complete Identity. Coordination does not
replace a LangGraph checkpointer.

Hosts that own an asynchronous cleanup or preflight task can use `join_task(task)` to
wait for its definitive result without letting repeated caller cancellation interrupt
settlement. Caller cancellation is re-raised after the owned task finishes; an owned
task failure remains observable when no caller cancellation outranks it.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/agui/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
