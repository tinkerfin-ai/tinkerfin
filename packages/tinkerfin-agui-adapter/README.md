# TinkerFin AG-UI Adapter

## What it is

`tinkerfin-agui-adapter` converts caller-supplied Deep Agents or LangGraph v2 stream
parts into validated AG-UI 0.1.19 events. It does not create a graph, invoke a model,
query checkpoints, authenticate requests, or provide an HTTP server.

Use it when an application already owns graph execution and needs only the conversion
boundary. Applications that also need Graph binding, observed streams, and direct SSE
can use [`tinkerfin`](https://pypi.org/project/tinkerfin/).

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin-agui-adapter
```

## Quick Start

The converter accepts an asynchronous iterable of live v2 parts:

<!-- adapter-quick-start:start -->
```python
import asyncio

from ag_ui.core import RunAgentInput
from langchain_core.messages import AIMessageChunk

from tinkerfin_agui_adapter import astream_events, encode_sse


async def parts():
    yield {
        "type": "messages",
        "ns": (),
        "data": (
            AIMessageChunk(id="ai-1", content="Hello", chunk_position="last"),
            {"lc_agent_name": None, "langgraph_node": "model"},
        ),
    }
    yield {"type": "values", "ns": (), "data": {}, "interrupts": ()}


async def main():
    run_input = RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    async for event in astream_events(
        parts(),
        run_input=run_input,
    ):
        frame = encode_sse(event)
        print(frame, end="")


asyncio.run(main())
```
<!-- adapter-quick-start:end -->

Production graph integration supplies `messages`, `tasks`, and `values` with
`version="v2"` and `subgraphs=True`. The caller's complete `RunAgentInput` is preserved
on `RUN_STARTED.input`; graph input remains the caller's concern.
One conversion exclusively consumes and closes the supplied iterator.
`astream_events()` already creates the main lifecycle, including its unique terminal;
`AgUiLifecycleEventFactory` is for custom orchestrators that do not use this stream
facade. `encode_sse()` only renders an event and does not take lifecycle or transport
ownership.

## Event contract

- One conversion emits one `RUN_STARTED` and exactly one main terminal.
- Text, reasoning, and tool lifecycles close before state, interrupt, error, or
  completion boundaries.
- Full namespaces and IDs correlate concurrent messages, tool calls, results, and
  subagents; arrival order is not identity.
- Every `tasks/start` establishes a possible compiled-graph scope. Only a scope also
  correlated with a Deep Agents `task` Tool call receives `deep_agent_subagent`
  identity; ordinary graphs use `compiled_subgraph` provenance.
- Task and non-root values diagnostics use `source="langgraph.tasks"` and
  `source="langgraph.values"`. A LangGraph task is not automatically a subagent, and
  subgraph provenance never uses AG-UI `parentRunId`.
- Provider-private reasoning is removed from task, state, raw, message, and terminal
  payloads. `expose_reasoning_events=True` enables only verified event sources.
- `expose_subagent_events=False` suppresses public subgraph events while preserving
  validation.
- Child interrupts remain buffered until root `values` propagates an identical full ID
  and value. The terminal boundary publishes root state, then a root-first
  namespace-scoped message snapshot, then one interrupt outcome. A replay of the same
  child interrupt set must carry the identical message snapshot for that namespace.
- `prior_tool_call_ids` accepts complete `tf:tool:...` scoped IDs and lets a resumed
  host publish a child Tool result without synthesizing another start/args/end.
- Conversion pulls with bounded lookahead and closes an upstream iterator exposing
  `aclose()` on cancellation or early consumer exit.

`ResumeMapper.map()` translates complete AG-UI resume entries from native checkpoint
interrupts and messages grouped by full namespace. Resolved reviews require those
messages so Tool calls can be correlated safely. `ResumeMapper.map_agui()` instead
accepts complete AG-UI interrupts that the host persisted from an earlier terminal. It
reuses their already verified scoped `toolCallId` values and does not query a graph or
checkpointer. Never pass client-supplied interrupt payloads to that method.

Both paths distinguish resolved, abandoned, and mixed decisions and never convert
cancellation into rejection. `encode_sse(event, event_id=...)` encodes one event;
delivery, persistence, retries, and transport cancellation remain caller-owned.

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
