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
from tinkerfin_agui_adapter import astream_events


parts = graph.astream(
    graph_input,
    config,
    stream_mode=("messages", "tasks", "values"),
    version="v2",
    subgraphs=True,
)

events = astream_events(
    parts,
    run_input=run_input,
)

async for event in events:
    await send_event(event)
```

`astream_events()` creates `RUN_STARTED`, converts intermediate parts, and emits exactly one main terminal. It exclusively consumes and closes `parts`, so do not share that iterator with another consumer.

### Parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `parts` | required | Async LangGraph v2 stream |
| `run_input` | required | Complete AG-UI request |
| `expose_reasoning_events` | `False` | Emits supported reasoning events |
| `expose_subagent_events` | `True` | Delivers subagent events |
| `prior_tool_call_ids` | `frozenset()` | Complete scoped tool IDs emitted before resume |

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
adapter = DeepAgentAgUiAdapter(run_input.run_id)

await send_event(lifecycle.started(run_input=run_input))
try:
    async for part in parts:
        for event in adapter.process(part):
            await send_event(event)
    for event in adapter.finish():
        await send_event(event)
    await send_event(
        lifecycle.finished(
            thread_id=run_input.thread_id,
            run_id=run_input.run_id,
            outcome=adapter.main_outcome(),
        )
    )
except Exception:
    for event in adapter.abort(code="runtime_error"):
        await send_event(event)
    await send_event(
        lifecycle.failed(
            run_id=run_input.run_id,
            message="Agent run failed",
            code="runtime_error",
        )
    )
```

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

Next: [AG-UI usage reference](api-reference.md).
