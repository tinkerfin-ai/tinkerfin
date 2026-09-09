# TinkerFin Contracts

`tinkerfin-contracts` provides protocol-neutral run identities and Runtime observation
interfaces so integrations can share execution data through one common contract.

## Installation

```bash
pip install tinkerfin-contracts
```

## Run identity

```python
from tinkerfin_contracts import RunIdentity

identity = RunIdentity(threadId="thread-1", runId="run-1")
```

Both canonical identifiers are non-empty, immutable, and limited to 1,024 characters.

## Runtime observers

`RuntimeObserver.open_run()` creates one request-scoped
`RunObservationSession`. The session receives validated run and Native observation
models, owns any work it starts, exposes asynchronous failure notification, and closes
idempotently at the Runtime boundary.

`RunSourceContext` carries the canonical identity, ordinary/branch/resume/abandon input
kind, parent lineage, Runtime mode, finite input/config snapshots, public resume
summaries, and Runtime-owned private state keys.

The observation union contains:

- Run start, input, resume-checkpoint, Observer-failure, terminal, and close values;
- validated Native message, task, root/subgraph state, interrupt, and declared extra-mode
  values;
- stable full namespace, IDs, UTC time, and monotonic clock evidence.

Runtime observers receive validated models rather than captured Python `repr`.
JSON fields reject non-finite numbers during Python and JSON validation. Model fields
cannot be reassigned, but nested dictionaries and lists remain mutable. Treat received
evidence as read-only; the Runtime supplies an independent copy to each observer.
`Command(resume=...)` remains invocation input and is represented only through the
protocol-neutral Run source summary and checkpoint Observation.

## License

Apache License 2.0. See
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
