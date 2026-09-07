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
    stream = await tinkerfin.open_run(
        RunIdentity(threadId="thread-1", runId="run-1"),
        agent=agent,
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for part in stream:
        print(part)


asyncio.run(main())
```

`create_deep_agent(...)` records the build call. `open_run()` resolves the Definition,
constructs the Graph asynchronously, binds identity and observations, and returns one
single-use managed stream.

Advanced integrations that need a reusable direct Runnable can continue with
[Direct Graph](#direct-graph).

## Core concepts

### Plan Mode

Plan Mode adds a standalone read-only Planning workflow and user review before an
approved task reaches the configured Deep Agent. Enable the capability on an immutable
TinkerFin factory; the installed Deep Agents factory signature does not change.

```python
from tinkerfin import TinkerFin


planned = TinkerFin().plan(
    enabled=True,
    default_mode="default",
    planner_model="openai:gpt-5.4",
)
agent = planned.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
    checkpointer=production_checkpointer,
)

plan_stream = await planned.open_run(
    RunIdentity(threadId="thread-1", runId="run-1"),
    agent=agent,
    input=graph_input,
    mode="plan",
)
default_stream = await planned.open_run(
    RunIdentity(threadId="thread-1", runId="run-2"),
    agent=agent,
    input=graph_input,
    mode="default",
)
```

Plan review defaults to `PlanReviewAction.APPROVE`, `RESPOND`, and `REJECT`.
`.plan(allowed_review_actions=...)` accepts an ordered, non-empty sequence of distinct
`PlanReviewAction` values and publishes exactly those decisions in the interrupt
response Schema. A host with a trusted draft editor can explicitly include
`PlanReviewAction.EDIT`; editing is not enabled by default.

Approval is the only review decision that exits Planning and starts native execution.
`REJECT` invalidates the current draft, accepts an optional reason, emits one visible
Planner reply, and waits for another Plan input. An explicitly configured `CANCEL`
performs the same Plan-mode continuation without a reason. These domain decisions are
resolved resume payloads; AG-UI `status="cancelled"` remains true abandonment and does
not invoke the model.

Selecting `mode="plan"` requires a concrete `BaseCheckpointSaver` and one explicit
model. `planner_model` is optional when the Agent definition supplies `model`; omitting
both is rejected. TinkerFin never creates an in-process saver or silently weakens
durability. Production hosts must supply a production-grade saver; the same saver,
Store, cache, backend, and runtime context are borrowed by Planning and native
execution. Planning and handoff boundaries use synchronous checkpoint durability, and
an explicit non-`sync` value is rejected.

`mode="default"` calls the native Deep Agent Graph directly without running Planning or
creating a parent Graph. Native Todo, Tool review, subagent, cancellation, and error
behavior remains available. AG-UI review batches may contain both resolved and abandoned
actions without requiring the host to modify Deep Agents state or middleware.

Only `ls`, `read_file`, `glob`, and `grep` are available to the Planner. Every
clarification question explicitly declares whether it is required and selects one semantic
answer type: `single_choice`, `multiple_choice`, `text`, `date`, `time`, or `datetime`. Choice questions can
accept one custom alternative, and optional questions can be explicitly skipped. Clients
submit stable option IDs or typed values under the checkpoint question ID; Planning derives
trusted option labels and canonical values from the checkpointed form. A skipped answer
remains explicit trusted Plan context rather than an omitted payload. Approval freezes a `ConfirmedPlan`
and commits a deterministic handoff bound to the original user message ID. The runtime
then starts the native Deep Agent in the same request; the effective mode becomes
`default` before execution. The public Plan state and value models are available from
`tinkerfin.plan` and are emitted under the root state key `tinkerfin_plan`.

Date answers use `YYYY-MM-DD` without a time zone. Time answers use one local wall-clock
minute in the question's IANA time zone; a question may declare inclusive `minimum` and
`maximum` bounds, and ranges cannot wrap across midnight.
Datetime answers combine one calendar date and local minute in the question's required
IANA zone. Their optional local `minimum` and `maximum` can span dates. Normalization
retains `localDateTime` and `timeZone` and adds the unique UTC `instant`; DST gaps and
repeated local minutes are rejected instead of silently selecting an offset.

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

The native Graph composes application, Definition, and upstream state contracts. The
standalone Planning Graph composes the fields it needs without changing native default
topology. A conflicting field fails before a Plan run.
Runtime context continues to use Deep Agents `context_schema`; it is not merged into
state or stored in the checkpoint.

### Direct Graph

```python
graph = await agent.create_graph(mode="default")
result = await graph.ainvoke(graph_input, config=config)
```

The direct Graph supports `ainvoke()`, `astream()`, `abatch()`, and standard Runnable
composition without managed identity, Observation, Trace, AG-UI, Messaging, or business
lifecycle. Direct resume uses LangGraph `Command(resume=...)`; synchronous execution is
intentionally rejected for async checkpointers, Stores, tools, and cancellation.

### Managed native runs

```python
parts = await tinkerfin.open_run(
    identity,
    agent=agent,
    input=graph_input,
    mode="plan",
    config=config,
    context=context,
    on_native_part=on_native_part,
)
```

The selected Runtime Profile constructs the Graph asynchronously and injects
`RunIdentity` plus its complete upstream invocation contract. Supported extra modes may
be added, but required semantic modes cannot be removed and state output must remain
complete. `NativeGraphRunStream` returns original upstream objects while transferring
each Driver-owned canonical frame exactly once to Observation, Native SSE, or Messaging.
It preserves ordering, backpressure, errors, cancellation, coordination, observer
ordering, and cleanup. `open_run()` returns after Run start and input observations are
durable, before the first model output is pulled. The caller must close a returned stream
that it will not iterate.

When only the final root state is needed, use the same managed lifecycle without
handling stream parts:

```python
state = await tinkerfin.ainvoke(
    identity,
    agent=agent,
    input=graph_input,
    mode="default",
)
```

This method consumes the canonical stream internally and retains Observation, Trace,
terminal settlement, and cleanup. Direct `graph.ainvoke(...)` remains the explicitly
unmanaged advanced boundary.

### Managed AG-UI runs

```python
from tinkerfin import AgUiResumeRequest, RunIdentity


events = await tinkerfin.open_agui_run(
    RunIdentity(threadId="thread-1", runId="run-1"),
    agent=agent,
    messages=[{"id": "message-1", "role": "user", "content": "Hello"}],
    parent_run_id=parent_run_id,
    mode="default",
    config=config,
    context=context,
    on_native_part=on_native_part,
    on_agui_event=on_agui_event,
)
```

The AG-UI path uses the same selected Profile and canonical frame as the native path.
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
Messaging. Give `open_agui_run()` only the next request's client decisions; it resolves
trusted interrupts and Tool correlation from the Definition checkpointer:

```python
resume_identity = RunIdentity(threadId="thread-1", runId="run-resume")
events = await tinkerfin.open_agui_run(
    resume_identity,
    agent=agent,
    resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    parent_run_id=parent_run_id,
    config=config,
    context=context,
    on_resume_saved=record_checkpoint_idempotently,
    on_resume_not_saved=release_unprepared_claim_idempotently,
)
```

Before continuing, the selected Profile durably accepts the exact interrupted checkpoint
without running a node or discarding root, Planning, or subgraph pending work.
`on_resume_saved` runs only after that acceptance is readable and may receive the same
`AgUiResumeCheckpoint` on retry, so it must be idempotent. If setup fails before durable
acceptance, `on_resume_not_saved` settles the host claim through protected cleanup. An
entirely cancelled request emits a finite cancelled lifecycle without invoking a Graph.

Advanced integrations that own a trusted event log or need custom request orchestration
can still use `new()`, `new_agui()`, `prepare_agui_resume()`, `AgUiResumeBinding`, and
`failed_agui_run()`. These low-level boundaries preserve the same capabilities but make
the caller responsible for their ordering and settlement; they are not required by the
ordinary managed path.

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
input, final model requests, provider and Tool call lifecycles, resume checkpoint,
terminal, close, and validated Native `messages/tasks/values` facts before `on_part` and
AG-UI conversion. Model and Tool callbacks do not wait for Native response delivery;
Native facts remain authoritative for messages, Todo, Plan, HITL, state, checkpoints,
and subagents. Observer failure terminates the Run fail-closed, while Runtime still
settles and closes every opened session. Registration is preserved by `.plan(...)`;
registering the same Observer object twice is rejected.

A failure before Agent execution uses `runtime_initialization_error` in its terminal
Observation. The original exception remains on the stream, and no Model or Tool call
is invented. Cancellation and Observer failures retain their own classifications.

Synchronous and asynchronous Tools both support observers. A call's start observation
is settled before execution; a rejected start prevents the call from running.
Cancelling a Run cannot forcibly stop a synchronous function that has already started.
Such functions must bound their own blocking I/O and release resources on completion.

Runtime records provider and Tool callbacks, not generic chain or middleware callbacks.
Middleware still executes in its declared Deep Agents order, and its final model requests
and actual Tool executions remain observable. A reusable middleware or Tool can publish
an explicit Memory, Guardrail, retrieval, or custom action with
`async with trace_contribution(kind="memory", name="Customer memory")` without
depending on a Tracer implementation. SkillsMiddleware discovery is not a Trace event;
when a model reads `SKILL.md`, the operation is recorded only as its ordinary `read_file`
Tool execution.

Interrupt, cancellation, abandonment, and generator closure are control flow rather
than failures. Runtime settlement closes unmatched callback work without assigning one
Run failure to unrelated events.

The Runtime selects the Agent outcome before terminal broadcast. If an Observer rejects
that already selected terminal, callers still fail closed and healthy Observers receive
the failure notice, but the actual Agent outcome is not rewritten after execution ended.

`open_agui_run()` converts ordinary model, Sandbox, Definition, and Graph setup failures
into the same observed failed lifecycle. Advanced orchestrators that accept a Run before
calling the managed facade can use `failed_agui_run(...)` directly.

The Observation contracts live in `tinkerfin-contracts`. The provided semantic Ledger
implementation is `tinkerfin-tracing`; it records Runtime and Native facts, not AG-UI,
Messaging, SSE, or Redis delivery state.

Provider reasoning persistence requires two independent opt-ins. Configure a verified
extractor on the Native Driver and content retention on the Tracer:

```python
from langchain_core.messages import BaseMessage
from pydantic import JsonValue

from tinkerfin import (
    DeepAgentsV2RuntimeProfile,
    ReasoningExtractor,
    TinkerFin,
    TinkerFinStreamProtocolError,
)
from tinkerfin_tracing import ReasoningCapturePolicy, Tracer


class HostReasoningExtractor:
    @property
    def name(self) -> str:
        return "acme.reasoning"

    def extract(
        self,
        message: BaseMessage,
        *,
        provider: str | None,
    ) -> JsonValue | None:
        if provider != "acme":
            return None
        value = message.additional_kwargs.get("acme_reasoning")
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise TinkerFinStreamProtocolError("acme reasoning must be a string")
        return value


extractor: ReasoningExtractor = HostReasoningExtractor()
profile = DeepAgentsV2RuntimeProfile(
    reasoning_extractors=(extractor,),
)
tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
tinkerfin = TinkerFin(runtime_profile=profile).observe(tracer)
```

Without an extractor, Runtime emits no reasoning observation. Without the independent
content policy, Tracing records only explicit omission and stores neither content nor a
digest. Business fields named `reasoning_content` outside provider metadata remain
ordinary business data. TinkerFin does not provide a vendor-specific extractor. A
host-supplied `ReasoningExtractor` implements `extract(message, *, provider)` and must
return content only for a provider and source shape it can verify.

### Ownership

A Definition can create reusable direct Graphs and multiple managed streams. The selected
Runtime Profile always owns the exact factory. A Profile may expose native asynchronous
construction; otherwise Core invokes that Profile's synchronous factory in AnyIO's
capacity-limited worker boundary. Callers do not create their own Graph-construction
thread.

TinkerFin and every Definition borrow caller-provided models, checkpointers, Stores,
backends, caches, and Sandbox resources. They do not set up or close those resources.
Managed streams own only request-scoped execution, observations, coordination, and
cleanup.

A configured coordinator receives the same complete RunIdentity. Coordination does not
replace a LangGraph checkpointer.

Hosts that own an asynchronous cleanup or preflight task can use `join_task(task)` to
wait for its definitive result without letting repeated caller cancellation interrupt
settlement. Caller cancellation is re-raised after the owned task finishes; an owned
task failure remains observable when no caller cancellation outranks it.

## Advanced Runtime Profile integration

`TinkerFin()` uses the stable `DeepAgentsV2RuntimeProfile` by default. Select
`DeepAgentsV3RuntimeProfile` explicitly to use LangGraph's experimental v3 event stream.
A Profile owns the complete third-party integration: Graph construction, invocation
options, live-object validation, canonical observations, finite replay, and resume marker
writes that preserve interrupted Graph control. TinkerFin selects it before Definition
creation and records its
`profile_id` with durable lineage; branch and resume reject a checkpoint created by
another Profile before Graph continuation.

The Profile identity is framework-private integration and recovery metadata. Hosts select
the integration during framework assembly, but must not copy `profile_id` into business
database models, request payloads, or application response schemas.

Both built-in Profiles feed the same protocol-neutral Runtime boundary. There is no
automatic detection, negotiation, or fallback:

```python
from tinkerfin import DeepAgentsV3RuntimeProfile, TinkerFin


profile = DeepAgentsV3RuntimeProfile()
tinkerfin = TinkerFin(runtime_profile=profile)
```

Hosts with another real integration can implement `DeepAgentsRuntimeProfile` and inject
it explicitly.

The Runtime does not infer or negotiate a Profile from stream data. Every Profile must
produce the same current `NativeStreamFrame` contract so observers, AG-UI, Native SSE,
and Messaging remain independent of its upstream envelope. A custom Profile declares
its stable build and stream signatures, returns `DeepAgentsFactoryPreparation` for
factory-specific overrides, and owns its concrete resume staging and marker recognition.
It may additionally implement `create_agent_graph(factory, args, kwargs)` when graph
construction is natively asynchronous. Without that method, Core awaits the Profile's
selected synchronous factory through the bounded worker boundary; it never substitutes
the built-in v2 factory.
Factory failures become a managed lifecycle; cancellation and process control settle
marker-aware host claims before propagating. Built-in v2 behavior is never a fallback
for another Profile.

The built-in v2 Profile stores lineage and resume acceptance in private checkpoint
channels. Its Tool-review integration occupies the locked Deep Agents patch-middleware
slot, delegates approve/edit/reject/respond unchanged, and adds only the private
cancellation decision required to preserve mixed AG-UI batches. These details are
Profile implementation contracts and are not part of the ordinary managed API.

A lazy resumed Agent normally inherits the `TinkerFin(checkpointer=...)` saver. If its
Definition intentionally overrides that saver, pass the same object as
`resume_checkpointer` to `open_agui_run()`. This declares the durable marker authority
before host setup begins; a returned Definition using another object fails closed and
does not release the host claim.

The v3 Profile is experimental because the locked LangGraph API declares v3
experimental. The current LangGraph implementation internally drives that event stream
from its v2 object stream; this is an upstream implementation detail and does not create
a downstream version branch.

## User input

`open_agui_run(messages=...)` accepts standard AG-UI user messages with final,
distinct message IDs. It validates and converts their text, image, and document
content before creating the agent. Submit only the next user messages when a
checkpointer already owns conversation history. Invalid message content produces
one `RUN_STARTED` / `RUN_ERROR` lifecycle without creating the agent.
Use exactly one of `messages`, advanced native `input`, or `resume`.

Hosts assigning their own IDs can inspect an ID-free submission with
`AgUiUserInput` from `tinkerfin`. Its `text` and `attachment_ids` properties are
available before authorization. Load the referenced files through your authenticated
repository, then call `submission.with_attachments(authorized_files)` to replace
client metadata. The resulting `attachments` are typed descriptors for your own
transactional binding. Assign final IDs when constructing the standard messages
for persistence and `open_agui_run`; no placeholder ID or LangChain conversion is
required in the host. The framework never grants file access or opens storage.

## Attachments

Configure attachment access once on the factory:

```python
from tinkerfin import AttachmentSupport, TinkerFin

agents = TinkerFin().attachments(AttachmentSupport(read_image=read_authorized_image))
definition = agents.create_deep_agent(model=model)
```

The asynchronous resolver authorizes each attachment in its user or tenant scope and
returns `AttachmentImage(data=..., mime_type=...)`. The host owns storage and bounds
image bytes; report missing files with `FileNotFoundError`. Other failures and
cancellation propagate. `Attachment` describes a durable file with an opaque ID,
name, MIME type, and byte size; messages store its `content_block()` reference.

Root agents, the default general-purpose agent, declarative subagents, and Plan
planner/review calls use their own model's `profile.image_inputs` capability.
Only an explicit `True` enables images. For host-configured models, supply
`supports_images=lambda model: ...` to `AttachmentSupport`; the predicate must
check each destination model and return a boolean. Capability never grants access:
the resolver must authorize every requested attachment independently.

At most `max_images` distinct recent images are loaded per request (default 5).
Tool images follow the complete tool-result batch. Unsupported and missing images
remain explicit text references. Native filesystem tools, permissions, eviction,
and general-purpose profile settings remain active. Compacted conversation history
retains readable attachment IDs, names, and MIME types. Checkpoints contain durable
references; trace replaces request-only image bytes with their descriptors.

Automatic integration uses the built-in native Deep Agents factory. A decorated
or replaced factory is rejected before model or backend preparation; its callbacks
are never bypassed. Use runtime observers for supported observation, or configure
attachment middleware explicitly in caller-owned custom graphs.

Compiled and remote custom agents own their attachment access and do not inherit
the parent's resolver. Configure their own TinkerFin factory, or install
`support.middleware()` after model-routing middleware in an independently
constructed LangChain agent. Hosts should provide bounded document-reading and
image-reading tools for references reopened after context compaction.

Tools explicitly declared with `metadata={"read_only": True}` are available to the
Plan planner; hosts must apply that marker only to operations safe for read-only
planning. Unmarked tools remain excluded from the planning action space.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/agui/index.md)
- [Tracing guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/tracing/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
