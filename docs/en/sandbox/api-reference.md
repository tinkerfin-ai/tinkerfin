# Sandbox usage reference

[Sandbox basics](index.md) · [中文](../../zh/sandbox/api-reference.md)

## Configuration and connection

### `OpenSandboxConfig`

| Field | Default | Purpose |
| --- | --- | --- |
| `image` | Version-pinned TinkerFin image | Image for new Sandboxes |
| `entrypoint` | `/opt/sandbox-runtime/bin/entrypoint.sh` | Container entry command |
| `env` | `{}` | Sandbox environment variables |
| `metadata` | `{}` | Application metadata; reserved ownership fields are rejected |
| `resource` | `cpu=1, memory=2Gi` | Resource request |
| `volumes` | `()` | OpenSandbox volumes |
| `ttl` | 2 hours | Positive Sandbox lifetime |
| `lifecycle_request_timeout` | 10 minutes | Create, connect, and destroy request limit |
| `ready_timeout` | 5 minutes | Wait for a new Sandbox to become ready |
| `connect_timeout` | 30 seconds | Data-plane connection limit |
| `command_timeout` | 3600 seconds | Default non-negative command timeout |
| `workspace_root` | `/workspace` | Rooted file-tool directory; `None` disables rooted view |
| `health_command` | `printf ok` | Data-plane health command |
| `warm_pool_size` | `1` | Non-negative warm capacity |
| `command_env` | `{}` | Environment added to each Shell command |
| `enable_capture_offload` | `False` | Allow large command output to be saved to a file |

### `OpenSandboxClient`

| Parameter | Default | Purpose |
| --- | --- | --- |
| `connection_config` | required, may be `None` | Explicit OpenSandbox connection or SDK environment configuration |
| `config` | `None` | TinkerFin Sandbox settings |
| `initializers` | `()` | Async functions run for new ready instances |

Public methods are `create(metadata=None)`, `connect(sandbox_id)`, `inspect(sandbox_id)`, `destroy(sandbox_id)`, and `aclose()`. All are asynchronous.

## Manager

See [Sandbox lifecycle](lifecycle.md) for constructor parameters and operations. `build_agent_middleware(backend, permissions=None)` returns middleware ready for Deep Agents.

`await manager.check_ready()` returns `None` only when configured warm capacity is
verified. It raises `OpenSandboxWarmPoolUnavailableError` for startup or background
capacity failure and `OpenSandboxManagerClosedError` after shutdown.

## Backends and handles

| API | Use |
| --- | --- |
| `OpenSandboxBackend` | One connected asynchronous OpenSandbox data plane |
| `OpenSandboxHandle` | Stable borrowed handle across remote replacement |
| `RootedOpenSandboxBackend` | Maps virtual `/` into the configured workspace root |
| `build_rooted_filesystem_middleware(...)` | Builds matching middleware without a manager |

For a custom client that constructs these values directly, `OpenSandboxBackend` accepts the native `sandbox`, `default_timeout=60`, optional `command_env`, optional `working_directory`, `health_command="printf ok"`, and `enable_capture_offload=False`. `OpenSandboxHandle` accepts a backend; `RootedOpenSandboxBackend` accepts a handle and `root="/workspace"`.

Common asynchronous methods:

| Category | Methods |
| --- | --- |
| Shell | `aexecute(command, timeout=None)` |
| Files | `aread`, `awrite`, `aedit`, `adelete`, `als`, `aglob`, `agrep` |
| Transfer | `aupload_files`, `adownload_files` |
| Large output | `aexecute_with_offload` |
| Lifecycle | `arenew(timeout)`, `aget_runtime_info()`, `akill()`, `aclose()` |

`RootedOpenSandboxBackend.to_shell_path(file_path)` converts a virtual path for Shell. Prefer middleware-managed mapping in ordinary applications.

Synchronous remote methods fail explicitly; use the asynchronous forms.

## State implementations

| API | Use |
| --- | --- |
| `OpenSandboxState` | Custom binding, lease, warm-pool, and cleanup protocol |
| `InMemoryOpenSandboxState(namespace="")` | Current-process state |
| `SQLAlchemyOpenSandboxState(...)` | Shared SQLite or MySQL state |
| `get_sqlalchemy_opensandbox_state_schema(dialect=...)` | Generate complete schema DDL |
| `SQLAlchemyOpenSandboxStateSchema` | Immutable dialect, table names, and DDL |

`OpenSandboxInitializer` is the asynchronous initializer callable type that receives a newly ready backend.

### Immutable claims and bindings

| Type | Fields |
| --- | --- |
| `OpenSandboxBinding` | `sandbox_id`, `generation` |
| `OpenSandboxOwnerClaim` | owner key, digest, token, generation, optional binding |
| `OpenSandboxWarmClaim` | slot, token, generation |
| `OpenSandboxReadyWarmClaim` | warm claim fields plus the published Sandbox ID |
| `OpenSandboxCleanupClaim` | sandbox ID, token, generation |

These types mainly support custom `OpenSandboxState` implementations.

## Runtime information

| Model | Main fields |
| --- | --- |
| `OpenSandboxStatusInfo` | `state`, optional reason/message/last transition time |
| `OpenSandboxPlatformInfo` | `os`, `arch` |
| `OpenSandboxRuntimeInfo` | ID, availability, health, status, times, image, platform, metadata, unavailable reason |
| `OpenSandboxDetails` | RuntimeInfo plus `owner_key` and `cached` |
| `OpenSandboxUnavailableReason` | `not_found` or `unreachable` |

## Errors

| Error | Meaning |
| --- | --- |
| `OpenSandboxStateError` | Base state failure |
| `OpenSandboxStateOwnershipError` | Claim expired, was replaced, or belongs to another worker |
| `OpenSandboxStateConfigurationError` | Unsupported state, database, or schema configuration |
| `OpenSandboxDestroyError` | Remote destruction could not settle reliably |
| `OpenSandboxResetError` | No safe workspace root or reset failed |
| `OpenSandboxHandleOwnershipError` | Backend ownership is no longer valid |
| `OpenSandboxManagerClosedError` | A closed manager was used |
| `OpenSandboxSettlementTimeoutError` | Caller wait for protected close expired |
