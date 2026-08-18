# TinkerFin runtime guide

TinkerFin binds a caller-owned asynchronous source factory to one native or AG-UI
object stream. It does not construct or own the graph and does not mirror the
`graph.astream(...)` signature. The host remains responsible for request mapping,
checkpoint lookup, resume commands, HTTP status handling, and application resources.

## Construction and ownership

```python
from tinkerfin import TinkerFin

tinkerfin = TinkerFin(run_coordinator=None)
```

`TinkerFin` is an application-scoped stateless factory. It has no asynchronous context
manager. A configured `RunCoordinator` is borrowed and must be opened and closed by
its owner.

Each logical request creates a `TinkerFinRun`:

```python
run = tinkerfin.run(
    lambda: graph.astream(
        graph_input,
        config,
        context=context,
        stream_mode=("messages", "tasks", "values"),
        print_mode=(),
        output_keys=None,
        interrupt_before=None,
        interrupt_after=None,
        durability=None,
        control=None,
        subgraphs=True,
        debug=None,
        version="v2",
    ),
    principal=principal,
    on_part=on_part,
)
```

The source factory is synchronous and zero-argument. It returns a fresh, unconsumed
`AsyncIterator` when the selected object stream is first pulled. Runtime closes that
iterator on success, failure, cancellation, or early consumer exit. Graphs, models,
checkpointers, stores, and values captured by the closure remain caller-owned.

One `TinkerFinRun` creates exactly one object stream. Calling `astream()` after
`astream_agui()`, or claiming the same path twice, raises `RuntimeError`. Create a new
run for each HTTP request or independent consumer.

`on_part(part)` is an asynchronous observer. It completes before the part is delivered
or converted, cannot replace the part, and participates in error and cancellation
semantics.

## Native object streams

```python
parts = run.astream()

async for part in parts:
    ...
```

`astream()` accepts no parameters because every third-party invocation parameter is
already present in the source factory. The stream is pull-based and preserves ordering,
backpressure, source exceptions, cancellation, coordinator lifetime, and upstream
cleanup.

## AG-UI object streams

```python
events = run.astream_agui(
    thread_id=agent_input.thread_id,
    run_id=agent_input.run_id,
    parent_run_id=agent_input.parent_run_id,
    timeout=None,
    settlement_timeout=None,
    expose_reasoning_events=False,
    expose_subagent_events=True,
    prior_tool_call_ids=frozenset(),
    on_event=on_event,
)

async for event in events:
    ...
```

- `thread_id`, `run_id`, and optional `parent_run_id` are caller-provided AG-UI
  identity. `parent_run_id` represents run lineage, never subgraph nesting.
- `timeout` is one total pull deadline for the AG-UI stream.
- `settlement_timeout` limits only one caller's close wait. `None` waits without a
  deadline; a finite expiry raises `AgUiSettlementTimeoutError` while the same owned
  close task continues and remains available to another `aclose()` call.
- `expose_reasoning_events` enables only verified public reasoning events; private
  provider metadata remains stripped from public payloads.
- `expose_subagent_events` controls delivery of validated non-root events.
- `prior_tool_call_ids` contains complete scoped `tf:tool:...` IDs published before a
  resume request. Raw native IDs are rejected.
- `on_event(event)` is awaited before delivery and cannot replace the event.

The stream emits `RUN_STARTED` once and one main terminal. `abort()` returns the
remaining cancellation tail, including the unique `RUN_ERROR(code="cancelled")` when
appropriate. `aclose()` closes conversion and upstream ownership without claiming a
successful run.

### Native stream requirements

Strict AG-UI binding requires all three v2 modes and subgraph output:

```python
stream_mode = ("messages", "tasks", "values")
version = "v2"
subgraphs = True
```

The default `TinkerFin.run(source_factory)` path remains parameter-neutral and accepts
arbitrary native objects. Runtime does not reflect its opaque closure; AG-UI conversion
validates the envelopes that arrive and rejects malformed modes, message tuples, task
phases, values payloads, and unregistered namespaces.

For synchronous configuration preflight, bind an optional strict source and pass it to
the same `run()` method:

```python
from tinkerfin import AgUiNativeStreamConfig

run = tinkerfin.run(
    AgUiNativeStreamConfig(extra_modes=("custom",)).bind(
        graph.astream,
        graph_input,
        config,
        context=context,
        durability="sync",
    ),
    principal=principal,
    on_part=on_part,
)
```

The strict source fixes the required modes, `version="v2"`, and `subgraphs=True`.
`extra_modes` accepts `updates`, `checkpoints`, `debug`, and `custom`; required,
duplicate, or unsupported entries fail before calling the Graph boundary, creating or
pulling its iterator, acquiring coordination, observing a part, or emitting
`RUN_STARTED`. The bound Graph invocation remains lazy after validation.

Strict `run(...).astream()` returns a profiled `NativeGraphRunStream` for name-only
Messaging inference. All seven modes use the same `NativeStreamPart` codec and native
SSE shape. Generic native streams remain profile-free and require an explicit codec for
durable delivery.

### Compiled subgraphs and Deep Agents delegates

Every `tasks/start` establishes a possible child graph scope. That evidence is
independent from Deep Agents delegation:

- an ordinary child graph uses `compiled_subgraph` provenance;
- a scope also correlated with a Deep Agents `task` Tool call uses
  `deep_agent_subagent` provenance and adds its agent name, full description, and
  scoped parent Tool Call ID;
- every runtime task remains a sanitized `RAW` event from `langgraph.tasks`;
- non-root values remain sanitized provenance from `langgraph.values` and never
  overwrite root state.

Message fragments are correlated by full namespace, message ID, and chunk index. Tool
results are correlated by full namespace and native Tool Call ID. Parallel Tools,
reversed result order, and subagent input/output therefore retain their identities.

### Child interrupts

The adapter buffers a child interrupt until root `values` propagates an identical full
interrupt ID and value. Missing or conflicting propagation fails closed. At the root
boundary, output order is:

```text
open lifecycle END events
STATE_SNAPSHOT                  # root state only
MESSAGES_SNAPSHOT               # root first, then related child scopes
RUN_FINISHED(outcome=interrupt)
```

The message snapshot uses the same namespace-scoped message and Tool IDs as live
events. Relevant child scopes follow registration order, and identical scoped entries
are deduplicated without merging different namespaces.

## Resume mapping

AG-UI resume entries are input to a later HTTP request. The host loads pending
interrupts and checkpoint messages, groups messages by full namespace, and invokes the
adapter's pure mapper:

```python
from langgraph.types import Command
from tinkerfin_agui_adapter import ResumeMapper

translation = ResumeMapper().map(
    entries=agent_input.resume or (),
    interrupts=pending_interrupts,
    messages_by_namespace={
        (): root_messages,
        child_namespace: child_messages,
    },
)

if translation.mode == "command":
    graph_input = Command(resume=translation.root)
```

`messages_by_namespace` is required whenever at least one review is resolved. Missing
checkpoint messages fail with `CHECKPOINT_MESSAGES_REQUIRED`; a wholly cancelled
abandonment does not require Tool-call correlation.

`command` means every decision can use stock Deep Agents resume data. `abandon`
preserves full cancellation without inventing a rejection. `custom` preserves mixed
resolved and cancelled slots for host middleware that can execute them losslessly.

Pass `frozenset(translation.prior_tool_call_ids)` to the resumed
`astream_agui()`. A subsequent child `ToolMessage` then publishes only
`TOOL_CALL_RESULT`; it does not duplicate start, args, or end events.

## Direct SSE

Native and AG-UI object streams render themselves:

```python
native_body = run.astream().to_sse(
    timeout=None,
    mapper=native_mapper,
    event_id_resolver=native_event_id_resolver,
)
```

```python
agui_body = run.astream_agui(
    thread_id=thread_id,
    run_id=run_id,
).to_sse(
    mapper=agui_mapper,
    event_id_resolver=agui_event_id_resolver,
)
```

With no mapper, native output is a versioned finite JSON object with
`event: stream-part`; AG-UI output is its validated protocol JSON. A custom async
mapper returns `SsePayload(data=..., event=..., retry=...)` or `None` to filter the
source item. A separate async ID resolver returns `str`, `int`, or `None`. All SSE
fields are validated before framing. CRLF and CR in custom data normalize to LF;
trailing LF is preserved by an empty final `data:` field, while other Unicode
separators remain ordinary data.

The body can be passed directly to Starlette or FastAPI:

```python
await agui_body.prepare(preflight=authorize_request)

return StreamingResponse(
    agui_body,
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
)
```

`prepare()` is optional. It runs static host preflight and closes on failure without
opening or pulling the source and without calling the mapper or ID resolver. Errors
after response headers are committed can only terminate the body, so request
validation that must select an HTTP status belongs before response construction.

## Durable Messaging

Direct SSE is not persisted. For ordered storage, replay, attachment, and remote
cancellation, give the unencoded object stream to an application-scoped channel:

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")

    events = run.astream_agui(
        thread_id=agent_input.thread_id,
        run_id=agent_input.run_id,
        parent_run_id=agent_input.parent_run_id,
    )
    body = await channel.sse(
        events,
        stream=agent_input.thread_id,
        run=agent_input.run_id,
        after=lambda: parse_last_event_id(request),
        attach_identity=agent_input,
        cancel=events.abort,
    )

    return StreamingResponse(body, media_type="text/event-stream")
```

A name-only channel infers `AgUiCodec` or `NativeStreamPartCodec` from immutable
source metadata before iteration. The handle is reusable across requests and streams;
each source is single-use. The channel name binds one codec family across every
stream and worker, and the backend verifies that binding at preparation and append.
Custom sources provide explicit `codec` and optional `renderer`.

`channel.sse()` is the high-level form of `channel.wrap(...)` followed by
`subscription.sse()`. Both complete backend preflight before returning. Durable SSE
IDs are committed channel sequence numbers and support exclusive `Last-Event-ID`
replay. Passing a pre-encoded Runtime SSE body is rejected.

Its `after` argument accepts `int`, `None`, or a zero-argument synchronous callback
returning `int | None`. The callback runs once before durable preparation, so parsing a
request cursor remains pre-response work. Callback errors and invalid results close the
unclaimed source. Lower-level `wrap()`, `validate_cursor()`, `wrap_recoverable()`, and
backend APIs continue to accept only a concrete `int | None`.

## Principal coordination

Use a coordinator when runs for the same application principal must serialize:

```python
from tinkerfin import InMemoryRunCoordinator, TinkerFin

coordinator = InMemoryRunCoordinator(key_resolver=lambda principal: principal)
tinkerfin = TinkerFin(run_coordinator=coordinator)

run = tinkerfin.run(source_factory, principal="user-1")
```

`InMemoryRunCoordinator` is process-local. `RedisRunCoordinator` coordinates workers
through leases. Coordination does not persist graph state and does not replace a
LangGraph checkpointer.

## Sandbox integration

Sandbox allocation is independent from Runtime. The host resolves its application key,
obtains a backend from `tinkerfin-sandbox`, injects it while constructing the graph,
and then binds each invocation through `TinkerFin.run(...)`. Runtime never owns or
closes the Sandbox manager.

## Related documentation

- [Core package](../packages/tinkerfin/README.md)
- [AG-UI adapter](../packages/tinkerfin-agui-adapter/README.md)
- [Messaging](../packages/tinkerfin-messaging/README.md)
