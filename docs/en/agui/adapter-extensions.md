# Use the AG-UI converter directly

[Interrupts and resume](interrupts-and-resume.md) · [中文](../../zh/agui/adapter-extensions.md)

Use `tinkerfin-agui-adapter` by itself when your application already creates and runs LangGraph and only needs native-v2-to-AG-UI conversion.

## Installation

```bash
pip install tinkerfin-agui-adapter
```

The converter does not create a Graph, invoke a model, read checkpoints, or provide an HTTP server.

## The simplest conversion path

```python
from tinkerfin_agui_adapter import RunIdentity, astream_events


parts = graph.astream(
    graph_input,
    config,
    stream_mode=("messages", "tasks", "values"),
    version="v2",
    subgraphs=True,
)

events = astream_events(
    parts,
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
)

async for event in events:
    await send_event(event)
```

`astream_events()` creates `RUN_STARTED`, converts intermediate parts, and emits exactly one main terminal. It exclusively consumes and closes `parts`, so do not share that iterator with another consumer.

### Parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `parts` | required | Async LangGraph v2 stream |
| `identity` | required | Main thread and run identity emitted by this conversion |
| `expose_reasoning_events` | `False` | Emits supported reasoning events |
| `expose_subagent_events` | `True` | Delivers subagent events |
| `prior_tool_call_ids` | `frozenset()` | Complete scoped tool IDs emitted before resume |
| `private_state_keys` | `frozenset()` | Top-level state channels omitted at known public projection boundaries |

`private_state_keys` is explicit for the standalone converter because it cannot infer
which host fields are private. It never deletes nested same-named fields. TinkerFin
Plan Definitions provide their internal keys automatically.

## Encode events as SSE

```python
from tinkerfin_agui_adapter import encode_sse


async for event in events:
    frame = encode_sse(event, event_id=next_event_id())
    await send_text(frame)
```

`event_id` may be a string or integer. `encode_sse()` only renders one event; it does not persist, retry, replay, or cancel delivery.

## Control the main lifecycle yourself

Only custom orchestrators normally need `DeepAgentAgUiAdapter` directly:

```python
from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
)


lifecycle = AgUiLifecycleEventFactory()
identity = RunIdentity(threadId="thread-1", runId="run-1")
adapter = DeepAgentAgUiAdapter(identity=identity)

await send_event(lifecycle.started(identity=identity))
try:
    async for part in parts:
        for event in adapter.process(part):
            await send_event(event)
    for event in adapter.finish():
        await send_event(event)
    await send_event(
        lifecycle.finished(
            identity=identity,
            outcome=adapter.main_outcome(),
        )
    )
except Exception:
    for event in adapter.abort():
        await send_event(event)
    await send_event(
        lifecycle.failed(
            identity=identity,
            message="Agent run failed",
            code="runtime_error",
        )
    )
```

`abort()` closes only child text, reasoning, and Tool lifecycles. The custom
orchestrator emits the one main `RUN_ERROR`, as shown above.

Your orchestrator owns one start and one terminal. Prefer `astream_events()` unless you truly need that ownership.

## Reduce very small deltas

`micro_batch()` combines adjacent text and tool-argument deltas without crossing lifecycle boundaries:

```python
from tinkerfin_agui_adapter import micro_batch


batched = micro_batch(events)
async for event in batched:
    await send_event(event)
```

## Preserve scoped IDs

```python
from tinkerfin_agui_adapter import ScopedIdCodec


codec = ScopedIdCodec()
public_id = codec.encode("tool", ("researcher",), "call-7")
kind, namespace, raw_id = codec.decode(public_id)
```

Store the complete scoped ID. Raw tool IDs may repeat in different namespaces.

## Parse framework extensions

Tool approval metadata is fixed by `ToolReviewInterruptMetadata` and schema
`tinkerfin.deepagents.tool-review`:

```python
from tinkerfin_agui_adapter import parse_tool_review_interrupt


review = parse_tool_review_interrupt(persisted_interrupt)
print(review.tool_name, review.original_args.root)
```

The parser validates the complete interrupt, native action group, action index,
decision policy, arguments, and scoped Tool ID. Unknown fields and client-supplied
interrupt metadata fail closed.

Deep Agents `task` calls publish `SubagentProvenance` with schema
`tinkerfin.subagent-provenance`. Its `subagentInvocationId` is stable across resume;
`requestRunId` identifies the main request carrying the current event. Parent task
results expose `relatedSubagentInvocationId`. These fields describe Agent nesting;
standard AG-UI `parentRunId` retains branch and time-travel lineage semantics.

Next: [AG-UI usage reference](api-reference.md).
