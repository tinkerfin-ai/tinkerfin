# tinkerfin

## What it is

`tinkerfin` adds request-scoped native streams and optional AG-UI streams to Deep Agents while keeping
the installed `create_deep_agent(...)` and `CompiledStateGraph.astream(...)` parameter
shapes. Streams can be sent directly as SSE or persisted by `tinkerfin-messaging`.

Models, tools, backends, checkpointers, stores, Sandbox resources, and request mapping
remain host-owned.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

The examples use LangChain's OpenAI model string, so install that provider adapter too:

```bash
pip install langchain-openai
```

Use the matching LangChain provider package when selecting another model vendor.

AG-UI protocol conversion is optional:

```bash
pip install "tinkerfin[agui]"
```

Redis coordination is optional:

```bash
pip install "tinkerfin[redis]"
```

Install both integrations with `pip install "tinkerfin[agui,redis]"`.

This extra provides `RedisRunCoordinator` and the reusable auto-renewing
`RedisLeaseLock` with monotonic fencing tokens.

## Quick Start

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new(identity=RunIdentity(threadId="thread-1", runId="run-1"))
    async for part in runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    ):
        print(part)


asyncio.run(main())
```

`create_deep_agent(...)` records the build call. `new()` creates a fresh Graph and a
single-use Runtime; the Graph iterator starts on first pull.

### Runtime Profile

`TinkerFin()` uses `DeepAgentsV2RuntimeProfile` by default. A Profile owns the complete
third-party integration: graph construction, invocation options, live-object validation,
canonical observations, finite replay, and the checkpointer writes that stage a resume
without changing interrupted Graph control. TinkerFin selects it before Definition creation
and records its `profile_id` with durable lineage; branch and resume reject a checkpoint
created by another Profile before Graph continuation.

Hosts with another real integration can implement `DeepAgentsRuntimeProfile` and inject
it explicitly:

```python
from tinkerfin import DeepAgentsV2RuntimeProfile, TinkerFin


profile = DeepAgentsV2RuntimeProfile()
tinkerfin = TinkerFin(runtime_profile=profile)
```

The Runtime does not infer a Profile from stream data, negotiate one during a Run, or
change Profiles across resume. Every Profile must produce the same current
`NativeStreamFrame` contract, so observers, AG-UI, Native SSE, and Messaging remain
independent of its upstream envelope. A custom Profile also declares the stable build
signature and bound `astream_signature` implemented by its graph, then returns
`DeepAgentsFactoryPreparation` for factory-specific HITL and permission overrides. The
stream signature lets resume construct the Graph lazily inside retained async ownership,
so factory failures and process control settle pre-marker host claims without exposing
build order. A Profile must also stage and recognize resume intent according to its
concrete checkpointer semantics. The built-in factory and v2 checkpoint behavior are
never used as runtime fallbacks for another Profile.

This distribution provides `DeepAgentsV2RuntimeProfile`; it does not provide a Deep
Agents v3 Profile or TodoGroups projection/UI. A new Profile is a concrete integration
that must emit the same canonical contract, not a downstream version branch or an empty
capability placeholder.

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
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
    mode="plan",
)
default_runtime = agent.new(
    identity=RunIdentity(threadId="thread-1", runId="run-2"),
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
    answer_type: Literal["acme:rating"] = "acme:rating"


class RatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    rating: int = Field(ge=1, le=5)


rating_type = clarification_type(
    type_id="acme:rating",
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

Custom type IDs are unversioned and namespaced. Their callbacks are synchronous,
deterministic, and free of external I/O. The host client supplies a renderer for each
custom type it enables. A Definition fingerprint binds the exact question Schema,
response Schema, validator description, and normalizer registration used by a checkpoint;
contract changes update the single current Definition and require existing data to be
rebuilt rather than introducing another type-ID version.

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
)
```

The selected Runtime Profile injects RunIdentity and its complete upstream invocation
contract into Graph config. Supported extra modes may be added, but required semantic
modes cannot be removed and state output must remain complete. `NativeGraphRunStream`
returns the original upstream objects while transferring each Driver-owned canonical
frame exactly once to Observation, Native SSE, or Messaging. It preserves ordering,
backpressure, errors, cancellation, coordination, observer ordering, and cleanup.

### AG-UI Runtime

```python
from tinkerfin import AgUiResumeRequest, RunIdentity


runtime = agent.new_agui(
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
    parent_run_id=parent_run_id,
    mode="default",
    on_part=on_part,
    on_event=on_event,
)

events = runtime.astream(graph_input, config, context=context)
```

The AG-UI Runtime uses the same selected Profile and canonical frame as Native Runtime.
Explicit upstream options must match that Profile; supported diagnostic modes may be
added, while incomplete state output is rejected. Invalid options fail before stream
side effects.

`RunIdentity` is the one canonical identity used by AG-UI lifecycle events, the Graph,
checkpoints, coordination, and Messaging. `RUN_STARTED.input` is omitted because Graph
input is already explicit. A non-empty `parent_run_id` resolves the unique completed
checkpoint leaf for that run in the same thread, so A→B followed by a request with parent
A creates A→C. Missing, cross-thread, active, failed, ambiguous, or improperly resumed
parents fail before Graph execution.

The returned `AgUiEventStream` preserves event, interrupt/resume, subagent, reasoning
privacy, cancellation, and cleanup semantics and can be passed directly to SSE or
Messaging. Give the Definition only the next request's client decisions; it resolves
trusted interrupts and Tool correlation from its concrete checkpointer:

```python
resume_identity = RunIdentity(threadId="thread-1", runId="run-resume")
binding = await agent.prepare_agui_resume(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    request=AgUiResumeRequest(entries=tuple(resume_entries)),
)
runtime = agent.new_agui(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
    on_resume_initialization_failed=release_unprepared_claim_idempotently,
)
events = runtime.astream(config=config, context=context)
```

The binding owns validated native resume, Tool correlation, cancellation, and provenance
facts and has a stable JSON round trip. It owns no identity or parent and exposes no
native `Command`. Before submitting the decision, the selected Profile durably writes the
private lineage and marker on the exact interrupted checkpoint without running a node or
discarding root, Planning, or subgraph pending work. `on_resume_checkpointed` runs only
after those writes are readable and may receive the same `AgUiResumeCheckpoint` again on
retry, so hosts must consume it idempotently. The decision-only continuation and accepted
retry remain internal; an accepted decision is not resubmitted. An entirely cancelled
binding emits a finite cancelled lifecycle without creating or invoking a Graph.
If failure, cancellation, or close occurs before the marker is readable,
`on_resume_initialization_failed` runs from the Runtime's retained settlement task so the
host can release its pre-checkpoint claim. It is never called after prepared or accepted
marker evidence exists, and the host callback must be idempotent.

AG-UI batches may mix resolved and cancelled Tool reviews. Resolved Tools retain native
behavior; cancelled Tools are removed from execution and receive a deterministic error
`ToolMessage` with `outcome="cancelled"` and `executed=False`. Main, automatic
general-purpose, declarative subagents, and permission-generated reviews use this
contract automatically. An externally compiled or remote subagent that can emit these
reviews must declare
`tinkerfin_hitl_contract=TINKERFIN_HITL_CONTRACT` in its subagent spec.

Plan Runtimes automatically remove their internal top-level state channels at every
public state boundary while preserving nested same-named business data.

### Runtime Observation

Register protocol-neutral observers immutably before creating a Definition:

```python
from tinkerfin import TinkerFin
from tinkerfin_tracing import Tracer


tracer = Tracer()
tinkerfin = TinkerFin().observe(tracer)
agent = tinkerfin.create_deep_agent(model=model, tools=tools)
```

Each admitted request opens one managed `RunObservationSession`. Runtime publishes real
input, resume checkpoint, terminal, close, and validated Native `messages/tasks/values`
facts before `on_part` and AG-UI conversion. Observer failure terminates the Run
fail-closed, while Runtime still settles and closes every opened session. Registration
is preserved by `.plan(...)`; registering the same Observer object twice is rejected.

The Runtime selects the Agent outcome before terminal broadcast. If an Observer rejects
that already selected terminal, callers still fail closed and healthy Observers receive
the failure notice, but the actual Agent outcome is not rewritten after execution ended.

When host setup fails after accepting a Run, use
`tinkerfin.failed_agui_run(error, identity=..., input=..., config=..., resume=...)` to
produce the same observed failed lifecycle without invoking a Graph.

The Observation contracts live in `tinkerfin-contracts`. The provided semantic Ledger
implementation is `tinkerfin-tracing`; it records Runtime and Native facts, not AG-UI,
Messaging, SSE, or Redis delivery state.

Provider reasoning persistence requires two independent opt-ins. Configure a verified
extractor on the Native Driver and content retention on the Tracer:

```python
from tinkerfin import (
    DeepAgentsV2RuntimeProfile,
    DeepSeekReasoningExtractor,
    TinkerFin,
)
from tinkerfin_tracing import ReasoningCapturePolicy, Tracer


profile = DeepAgentsV2RuntimeProfile(
    reasoning_extractors=(DeepSeekReasoningExtractor(),),
)
tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
tinkerfin = TinkerFin(runtime_profile=profile).observe(tracer)
```

Without an extractor, Runtime emits no reasoning observation. Without the independent
content policy, Tracing records only explicit omission and stores neither content nor a
digest. Business fields named `reasoning_content` outside provider metadata remain
ordinary business data.

### Ownership

A Definition may create multiple Runtimes; each one owns a separate Graph invocation
and one object stream. Graph construction is synchronous, so async servers should use
their controlled thread boundary around `new()` or `new_agui()` when needed.

A configured coordinator receives the same complete RunIdentity. Coordination does not
replace a LangGraph checkpointer.

Hosts that own an asynchronous cleanup or preflight task can use `join_task(task)` to
wait for its definitive result without letting repeated caller cancellation interrupt
settlement. Caller cancellation is re-raised after the owned task finishes; an owned
task failure remains observable when no caller cancellation outranks it.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/agui/index.md)
- [Tracing guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/tracing/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
