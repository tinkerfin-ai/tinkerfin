# Rooted files and commands

[Sandbox lifecycle](lifecycle.md) · [中文](../../cn/sandbox/rooted-filesystem.md)

When `workspace_root` is set, the agent's virtual `/` maps to that physical Sandbox directory. With `/workspace`, an agent path such as `/src/app.txt` maps to `/workspace/src/app.txt` remotely.

```python
config = OpenSandboxConfig(workspace_root="/workspace")
```

## Why use a rooted workspace

- File tools cannot escape through `..`;
- symbolic links to outside paths are rejected;
- `reset()` removes children but keeps the root itself;
- the agent sees shorter virtual paths instead of physical layout.

Shell is a separate capability. Raw Shell commands are not automatically confined by the file-tool root, so govern dangerous commands with tool permissions and human approval.

## Common asynchronous file operations

```python
backend = await manager.get(key)

await backend.awrite("/notes.txt", "hello")
result = await backend.aread("/notes.txt")
await backend.aedit("/notes.txt", "hello", "hello world")
entries = await backend.als("/")
matches = await backend.aglob("**/*.txt", "/")
hits = await backend.agrep("hello", "/", glob="*.txt")
await backend.adelete("/notes.txt")
```

### Method parameters

| Method | Parameters | Use |
| --- | --- | --- |
| `aread()` | `file_path`, `offset=0`, `limit=2000` | Read text by line range |
| `awrite()` | `file_path`, `content` | Write complete text |
| `aedit()` | path, old text, new text, `replace_all=False` | Exact replacement |
| `adelete()` | `file_path` | Delete a path, never the virtual root |
| `als()` | `path` | List a directory |
| `aglob()` | `pattern`, `path=None` | Find paths |
| `agrep()` | pattern, optional path/glob/count | Search text |

Results may contain both normal data and an error description. In search and batch operations, inspect the error field as well as the returned entries.

## Run a command

```python
result = await backend.aexecute(
    "python -m pytest",
    timeout=300,
)
```

`timeout=None` uses the backend default. Commands start in the configured working directory, but Shell itself can access other Sandbox paths.

For large output, enable capture offload:

```python
config = OpenSandboxConfig(enable_capture_offload=True)

result = await backend.aexecute_with_offload(
    "python -m pytest -vv",
    "/captures/tests.txt",
    max_inline_bytes=32_000,
    max_capture_bytes=5_000_000,
    timeout=300,
)
```

| Parameter | Purpose |
| --- | --- |
| `capture_path` | Virtual destination for full output |
| `max_inline_bytes` | Maximum output bytes returned inline |
| `max_capture_bytes` | Optional complete capture limit |
| `timeout` | Command timeout in seconds |

## Upload and download

```python
uploads = await backend.aupload_files([("/input/data.csv", csv_bytes)])
downloads = await backend.adownload_files(["/output/report.json"])
```

Responses preserve input order. A confirmed invalid path affects only that item. Transport failures and uncertain results propagate instead of retrying a write that may already have happened.

Rooted transfers require Python 3, Linux procfs, and shared process visibility between command and filesystem services. The default TinkerFin Sandbox image provides this environment.

## Connect an AgentRuntime

```python
from deepagents import FilesystemPermission
from tinkerfin import TinkerFin

permissions = [
    FilesystemPermission(
        operations=["write"],
        paths=["/policies/private/**"],
        mode="deny",
    )
]

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .build(
        model=model,
        backend=manager.workspace(workspace_key),
        permissions=permissions,
    )
)
```

The Runtime prepares the rooted backend and its filesystem middleware together when the
run starts. Permission rules that interrupt instead of deny require a checkpointer.
Use `build_rooted_filesystem_middleware()` only in a caller-managed Deep Agents Graph.

Next: [Persistent state and extensions](persistence-and-extensions.md).
