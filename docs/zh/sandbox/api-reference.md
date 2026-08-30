# Sandbox 使用参考

[Sandbox 入门](index.md) · [English](../../en/sandbox/api-reference.md)

## 配置与连接

### `OpenSandboxConfig`

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `image` | TinkerFin 固定版本镜像 | 新 Sandbox 使用的镜像 |
| `entrypoint` | `/opt/sandbox-runtime/bin/entrypoint.sh` | 容器入口命令 |
| `env` | `{}` | Sandbox 环境变量 |
| `metadata` | `{}` | 创建时附加的业务 metadata；保留字段不能覆盖 |
| `resource` | `cpu=1, memory=2Gi` | 资源规格 |
| `volumes` | `()` | OpenSandbox volume 配置 |
| `ttl` | 2 小时 | Sandbox 生存时间，必须大于 0 |
| `lifecycle_request_timeout` | 10 分钟 | 创建、连接和销毁请求时限 |
| `ready_timeout` | 5 分钟 | 等待新 Sandbox ready 的时限 |
| `connect_timeout` | 30 秒 | 连接数据面的时限 |
| `command_timeout` | 3600 秒 | 默认命令超时，必须大于等于 0 |
| `workspace_root` | `/workspace` | 文件工具映射的受限根；`None` 表示不创建 rooted view |
| `health_command` | `printf ok` | 健康检查命令 |
| `warm_pool_size` | `1` | 预热实例数，必须大于等于 0 |
| `command_env` | `{}` | 每次 Shell 命令附加的环境变量 |
| `enable_capture_offload` | `False` | 是否允许大输出写入文件 |

### `OpenSandboxClient`

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `connection_config` | 必填，可传 `None` | OpenSandbox 连接配置；`None` 时使用 SDK 环境配置 |
| `config` | `None` | TinkerFin Sandbox 配置 |
| `initializers` | `()` | 新实例 ready 后依次执行的异步初始化函数 |

公共方法为 `create(metadata=None)`、`connect(sandbox_id)`、`inspect(sandbox_id)`、`destroy(sandbox_id)` 和 `aclose()`。这些方法都是异步的。

## Manager

`OpenSandboxManager` 的构造参数和操作见 [Sandbox 生命周期](lifecycle.md)。`build_agent_middleware(backend, permissions=None)` 返回可直接传给 Deep Agents 的 middleware 元组。

只有配置的预热容量已经通过真实验证时，`await manager.check_ready()` 才会正常返回。启动或后台容量
失败会抛出 `OpenSandboxWarmPoolUnavailableError`，Manager 关闭后会抛出
`OpenSandboxManagerClosedError`。

## Backend 和 handle

| API | 用途 |
| --- | --- |
| `OpenSandboxBackend` | 一条已连接的异步 OpenSandbox 数据面 |
| `OpenSandboxHandle` | 在远端实例替换后仍保持身份稳定的借用 handle |
| `RootedOpenSandboxBackend` | 把虚拟 `/` 映射到配置的 workspace root |
| `build_rooted_filesystem_middleware(...)` | 不使用 manager 时创建匹配的文件 middleware |

如果自定义 client 需要直接创建这些对象：`OpenSandboxBackend` 接收原生 `sandbox`、`default_timeout=60`、可选 `command_env`、可选 `working_directory`、`health_command="printf ok"` 和 `enable_capture_offload=False`；`OpenSandboxHandle` 接收 backend；`RootedOpenSandboxBackend` 接收 handle 和 `root="/workspace"`。

常用异步方法：

| 类别 | 方法 |
| --- | --- |
| Shell | `aexecute(command, timeout=None)` |
| 文件 | `aread`、`awrite`、`aedit`、`adelete`、`als`、`aglob`、`agrep` |
| 传输 | `aupload_files`、`adownload_files` |
| 大输出 | `aexecute_with_offload` |
| 生命周期 | `arenew(timeout)`、`aget_runtime_info()`、`akill()`、`aclose()` |

`RootedOpenSandboxBackend.to_shell_path(file_path)` 把虚拟路径转换成 Shell 可使用的物理路径。普通业务优先让 middleware 处理该映射。

同步远程方法会明确报错；始终使用 `a` 开头的异步版本。

## 状态实现

| API | 用途 |
| --- | --- |
| `OpenSandboxState` | 自定义绑定、租约、warm pool 和 cleanup 协议 |
| `InMemoryOpenSandboxState(namespace="")` | 当前进程内状态 |
| `SQLAlchemyOpenSandboxState(...)` | SQLite 或 MySQL 共享状态 |
| `get_sqlalchemy_opensandbox_state_schema(dialect=...)` | 生成完整建表 SQL |
| `SQLAlchemyOpenSandboxStateSchema` | 不可变的 dialect、table names 和 DDL |

`OpenSandboxInitializer` 是新 Sandbox ready 后接收 backend 的异步初始化函数类型。

### 不可变 claim 和 binding

| 类型 | 字段 |
| --- | --- |
| `OpenSandboxBinding` | `sandbox_id`、`generation` |
| `OpenSandboxOwnerClaim` | owner key、摘要、token、generation、可选 binding |
| `OpenSandboxWarmClaim` | slot、token、generation |
| `OpenSandboxReadyWarmClaim` | warm claim 字段和已发布 Sandbox ID |
| `OpenSandboxCleanupClaim` | sandbox ID、token、generation |

这些类型主要用于自定义 `OpenSandboxState`，普通 manager 使用者不需要手工创建。

## 运行信息

| 模型 | 主要字段 |
| --- | --- |
| `OpenSandboxStatusInfo` | `state`、可选 reason/message/last transition time |
| `OpenSandboxPlatformInfo` | `os`、`arch` |
| `OpenSandboxRuntimeInfo` | Sandbox ID、available、healthy、状态、时间、镜像、平台、metadata、不可用原因 |
| `OpenSandboxDetails` | RuntimeInfo 加 `owner_key` 和 `cached` |
| `OpenSandboxUnavailableReason` | `not_found` 或 `unreachable` |

## 错误

| 错误 | 含义 |
| --- | --- |
| `OpenSandboxStateError` | 状态层错误基类 |
| `OpenSandboxStateOwnershipError` | claim 已过期、被替换或不属于当前 worker |
| `OpenSandboxStateConfigurationError` | 状态配置、数据库或 schema 不支持 |
| `OpenSandboxDestroyError` | 远端销毁未能可靠完成 |
| `OpenSandboxResetError` | workspace 无安全根或重置失败 |
| `OpenSandboxHandleOwnershipError` | 使用了不再有效的 backend 所有权 |
| `OpenSandboxManagerClosedError` | manager 关闭后仍被使用 |
| `OpenSandboxSettlementTimeoutError` | 调用方等待安全关闭超过时限 |
