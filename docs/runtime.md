# TinkerFin runtime guide

TinkerFin adds request-scoped native and AG-UI streams to Deep Agents. The host still
owns models, tools, backends, checkpointers, stores, Sandbox resources, request mapping,
checkpoint lookup, and resume commands.

## Create an agent

```python
from ag_ui.core import RunAgentInput
from tinkerfin import TinkerFin

tinkerfin = TinkerFin(run_coordinator=coordinator)

agent = tinkerfin.create_deep_agent(
    model=model,
    tools=tools,
    backend=backend,
    checkpointer=checkpointer,
    store=store,
)
```

`create_deep_agent(...)` has the installed Deep Agents signature. It records the build
arguments but does not create a Graph. Every `new()` or `new_agui()` call creates a
fresh Graph and one single-use Runtime.

Graph construction is synchronous. Async servers should run `new()` or `new_agui()`
through their controlled thread boundary when construction must not occupy the event
loop.

## Native stream

```python
runtime = agent.new(
    principal=principal,
    on_part=on_part,
)

async for part in runtime.astream(
    graph_input,
    config,
    context=context,
    stream_mode="values",
    version="v2",
):
    ...
```

`runtime.astream(...)` has the installed `CompiledStateGraph.astream(...)` parameter
shape and forwards the call unchanged. It returns `GraphRunStream` and preserves
ordering, pull-based backpressure, errors, cancellation, early-close cleanup,
coordination, and `on_part` ordering.

The Graph iterator is created on first pull. Calling `astream(...)` twice on the same
Runtime raises `RuntimeError`.

## AG-UI stream

```python
agent_input = RunAgentInput.model_validate(request_payload)

runtime = agent.new_agui(
    principal=principal,
    on_part=on_part,
    run_input=agent_input,
    timeout=None,
    settlement_timeout=None,
    expose_reasoning_events=False,
    expose_subagent_events=True,
    on_event=on_event,
)

events = runtime.astream(
    graph_input,
    config,
    context=context,
)
```

The AG-UI Runtime uses these native stream settings when they are omitted:

```python
stream_mode = ("messages", "tasks", "values")
version = "v2"
subgraphs = True
```

Explicit values must satisfy the same contract. `stream_mode` may also include
`updates`, `checkpoints`, `debug`, or `custom`. Missing, duplicate, unknown, or
conflicting values fail before Graph iteration, coordination, observers, or
`RUN_STARTED`.

The returned `AgUiEventStream` keeps the existing lifecycle:

- one `RUN_STARTED` and one main terminal;
- the complete caller `RunAgentInput` on `RUN_STARTED.input`;
- balanced text, reasoning, and Tool events;
- full namespace and ID correlation for parallel Tools and subagents;
- root/subgraph state isolation;
- final state and message snapshots before an interrupt terminal;
- provider-private reasoning removed from public payloads;
- idempotent abort and deterministic upstream cleanup.

`abort()` returns the remaining cancellation tail and is also exposed structurally to
Messaging. `aclose()` releases resources
without fabricating success. `on_part` runs before conversion; `on_event` runs before
delivery. `run_input.parent_run_id` is run lineage, not subgraph nesting.

## Resume

The host maps AG-UI resume entries with current checkpoint messages:

```python
from tinkerfin import AgUiResumeBinding
from tinkerfin_agui_adapter import ResumeMapper

translation = ResumeMapper().map(
    entries=agent_input.resume or (),
    interrupts=pending_interrupts,
    messages_by_namespace=messages_by_namespace,
)

resume = AgUiResumeBinding.from_translation(
    run_input=agent_input,
    translation=translation,
)
```

When the host persists the complete interrupts emitted by the earlier AG-UI terminal,
it can reuse the correlation already validated by the adapter:

```python
translation = ResumeMapper().map_agui(
    entries=agent_input.resume or (),
    interrupts=persisted_interrupts,
)
resume = AgUiResumeBinding.from_translation(
    run_input=agent_input,
    translation=translation,
)
```

`persisted_interrupts` must come from the host's trusted event log, not from client
payloads. The host must load the complete current pending batch and require the resume
entries to cover it exactly before claiming any action. This path does not query a Graph
or require checkpoint messages.

Pass `resume=resume` to `new_agui(...)`, then call
`runtime.astream(resume.command, ...)`. The binding requires a pure resume Command,
the same complete `RunAgentInput`, and complete `tf:tool:...` IDs. A mismatched Graph
input fails before the Graph iterator and does not consume the Runtime. A resumed Tool
result keeps its original scoped ID without repeating Tool start, args, or end.
Cancellation remains abandonment; it is not converted into rejection.

## SSE and Messaging

Both object streams provide `to_sse()`:

```python
body = events.to_sse(
    mapper=event_mapper,
    event_id_resolver=event_id_resolver,
)
await body.prepare(preflight=authorize_request)
```

For persistence, replay, attachment, and remote cancellation, pass the unencoded stream
to `tinkerfin-messaging`:

```python
body = await channel.sse(
    events,
    stream=agent_input.thread_id,
    run=agent_input.run_id,
    after=lambda: parse_last_event_id(request),
    attach_identity=agent_input,
)
```

Direct SSE is not persisted. Durable SSE IDs are committed channel sequence numbers,
and `Last-Event-ID` replay is exclusive. Do not pass an encoded SSE body through a
Messaging codec.

When creating the Agent source is expensive, `DeferredMessageSource` can postpone its
opener until Messaging has selected the producer owner. Attachments and replays close
that source without creating the Agent. A deferred source can also declare its own
cancellation callback, so the `cancel=` argument is omitted.

Committed events can be read without handling encoded bytes:

```python
latest = await channel.latest_seq(stream=stream_id)
page = await channel.read(stream=stream_id, after=cursor, limit=1000)
subscription = await channel.follow(stream=stream_id, run=run_id, after=cursor)
```

`read()` and `follow()` return `DecodedMessage` values. The Channel validates the
persisted codec before decoding; an explicit codec or a profile already inferred in
the current process is required. `follow()` also binds the authoritative durable
generation before returning its subscription.

## Coordination and ownership

Without a coordinator, `principal` must be `None`. A configured coordinator requires a
principal for every Runtime. Coordination serializes application principals; it does
not persist Graph state or replace a LangGraph checkpointer.

The Definition borrows every object passed to `create_deep_agent(...)`. Runtime does
not open or close models, stores, checkpointers, Sandbox managers, or coordinators.

## Low-level sources

`TinkerFin.run(...)` remains available for custom asynchronous sources and existing
integrations. Deep Agents callers normally use `create_deep_agent(...).new/new_agui`.

## IDE signatures

Published `.pyi` files are generated from the installed Deep Agents and LangGraph
source:

```bash
uv run python packages/tinkerfin/scripts/generate_stubs.py
uv run python packages/tinkerfin/scripts/generate_stubs.py --check
```

## Related documentation

- [Core package](../packages/tinkerfin/README.md)
- [AG-UI adapter](../packages/tinkerfin-agui-adapter/README.md)
- [Messaging](../packages/tinkerfin-messaging/README.md)
