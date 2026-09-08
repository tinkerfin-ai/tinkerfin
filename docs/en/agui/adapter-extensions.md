# Use the AG-UI converter directly

[Interrupts and resume](interrupts-and-resume.md) · [中文](../../cn/agui/adapter-extensions.md)

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

## Durable images and documents

`Attachment` from `tinkerfin_contracts.media` contains `id`, `name`, `mime_type`,
and `size_bytes`. Its `content_block()` produces a LangChain `image` or `file`
block with `file_id`, `mime_type`, and `extras.attachment`. Store the reference in
messages; the host authorizes access and supplies image bytes through
`TinkerFin().attachments(AttachmentSupport(read_image=...))` when making model
requests. Each destination model uses its own image capability; custom compiled
agents configure their own attachment access.

Ordinary runtime callers pass standard user messages to `TinkerFin.open_agui_run(messages=...)`; the runtime owns validation and conversion. `AgUiUserInput` provides ID-free text/attachment inspection and authorized descriptor replacement for hosts assigning message IDs.

For custom adapter integrations, the low-level `user_message_to_langchain()` from `tinkerfin_agui_adapter.media` converts AG-UI
user input. Durable images use `type: "image"`, documents use `type: "document"`,
with `source: {"type": "url", "value": "attachment:<id>"}` and the descriptor in
`metadata`. Snapshots preserve that input shape.

Tool results and assistant snapshots expose descriptors in an `attachments`
array. Assistant streaming uses CUSTOM `tinkerfin.message.attachments`, with
`{"messageId": "<scoped-id>", "attachments": [...]}`, between the matching
TEXT_MESSAGE_START and TEXT_MESSAGE_END. Merge these additions by attachment ID;
snapshot arrays replace the prior descriptors. Use `rawEvent.source` to place
subagent output with its owning invocation.

`MessageAttachments` in `tinkerfin_agui_adapter.media` validates this CUSTOM
payload. The package includes `contracts/message-attachments.schema.json` and a
matching fixture captured through real LangChain tool invocation. Descriptors
contain no bytes, credentials, or temporary download URLs.

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

`AttachmentToolCallResultEvent`, `AttachmentAssistantMessage`, and
`AttachmentToolMessage` declare typed `attachments` fields.
`AttachmentMessagesSnapshotEvent` validates and serializes attachment-bearing history.
After generic AG-UI replay, call `parse_attachment_output_event(event)` to validate a
tool result or snapshot and obtain its public attachment types. The package also ships
`tool-call-result.schema.json`, `assistant-message.schema.json`, `tool-message.schema.json`,
and `messages-snapshot.schema.json` under `contracts/`; the shared fixture covers live
output and matching history.
