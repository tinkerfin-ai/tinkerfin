# Sandbox basics

[Documentation](../README.md) · [中文](../../zh/sandbox/index.md)

`tinkerfin-sandbox` connects OpenSandbox to Deep Agents. An agent can run commands and work with files in an isolated environment instead of using the application server directly.

## When to use it

- The agent runs Shell commands;
- the agent edits project files;
- users or projects need isolated workspaces;
- unhealthy Sandbox instances should reconnect or be replaced;
- several application workers share Sandbox bindings.

## Installation

```bash
pip install tinkerfin-sandbox
```

You also need a reachable OpenSandbox service. Configure its domain and API key explicitly or through the environment supported by the OpenSandbox SDK.

## Your first Sandbox-backed agent

```python
from deepagents import create_deep_agent
from opensandbox.config import ConnectionConfig
from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
)


client = OpenSandboxClient(
    connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
    config=OpenSandboxConfig(workspace_root="/workspace"),
)
manager = OpenSandboxManager[str](
    client=client,
    key_resolver=lambda key: key,
)


async with manager:
    backend = await manager.get("tenant-1/user-7")
    agent = create_deep_agent(
        model=model,
        backend=backend,
        middleware=manager.build_agent_middleware(backend),
    )
```

Acquire the backend before constructing the Graph.

## Choose what a key represents

TinkerFin does not force keys to mean users, threads, or projects. Pick a stable scope for your application:

```python
manager = OpenSandboxManager[tuple[int, int]](
    client=client,
    key_resolver=lambda key: f"org/{key[0]}/project/{key[1]}",
)
```

Keys resolving to the same string share one stable handle and serialized lifecycle transitions. Different resolved keys may proceed concurrently.

Do not put passwords or tokens in a key. Remote owner labels use digests, but keys can still appear in application logs or state storage.

## Resource lifetime

By default, the manager controls its client and state lifetime. Graphs borrow the backend returned by `get()` and should not close it independently.

```python
async with manager:
    backend = await manager.get(key)
    # Use the backend.
# The manager settles operations and closes local resources.
```

Use asynchronous methods for all remote operations: `aexecute()`, `aread()`, `awrite()`, and their peers. Synchronous remote methods fail explicitly.

## Next steps

- [Sandbox lifecycle](lifecycle.md)
- [Rooted files and commands](rooted-filesystem.md)
- [Persistent state and extensions](persistence-and-extensions.md)
- [Sandbox usage reference](api-reference.md)

