# Sandbox 生命周期

[Sandbox 入门](index.md) · [English](../../en/sandbox/lifecycle.md)

`OpenSandboxManager` 为每个业务 key 保存一个稳定 handle。远端 Sandbox 失效或被替换后，调用方仍可继续使用同一个 handle 对象。

## Manager 配置

```python
manager = OpenSandboxManager(
    client=client,
    key_resolver=lambda key: str(key),
    state=None,
    warm_pool_size=None,
    fail_on_startup_warmup_error=False,
    settlement_timeout=None,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `client` | 必填 | 创建、连接、检查和销毁 Sandbox 的异步 client |
| `key_resolver` | 必填 | 把应用 key 转成稳定、非空字符串 |
| `state` | `None` | 绑定和租约状态；默认使用内存状态 |
| `warm_pool_size` | `None` | 覆盖配置中的预热数量 |
| `fail_on_startup_warmup_error` | `False` | 预热失败时是否让 `start()` 直接失败 |
| `settlement_timeout` | `None` | 调用方等待 manager 关闭的最长秒数 |

warm-pool 数量必须是严格整数。命令与生命周期 timeout 必须是有限数值；布尔值会在 State 启动或
创建 task 前被拒绝。

## 常用操作

| 方法 | 作用 | 远端 ID 是否通常变化 |
| --- | --- | --- |
| `get(key)` | 创建、重连或复用健康 Sandbox | 仅必要时变化 |
| `reconnect(key)` | 与 `get()` 相同，便于表达“重新连接”意图 | 仅必要时变化 |
| `recreate(key)` | 创建替代实例，并安全退役旧实例 | 会变化 |
| `reset(key)` | 清空 workspace root 的内容 | 不变化 |
| `destroy(key)` | 销毁已知实例并移除绑定 | 被删除 |
| `delete(key)` | `destroy()` 的同义入口 | 被删除 |
| `is_healthy(key)` | 检查当前实例是否健康 | 不变化 |
| `get_details(key)` | 返回运行状态和 owner 信息 | 不变化 |
| `check_ready()` | 预热容量未通过真实验证时抛出异常 | 不变化 |

```python
backend = await manager.get(project_key)

if not await manager.is_healthy(project_key):
    backend = await manager.recreate(project_key)

details = await manager.get_details(project_key)
```

大多数请求只需要 `get()`。不要在每次请求前主动 `recreate()`，否则会失去复用和预热的意义。

## 启动和关闭

`async with manager` 会自动调用 `start()` 和 `aclose()`。需要手工控制时：

```python
await manager.start()
try:
    backend = await manager.get(key)
finally:
    await manager.aclose()
```

`start()` 可以重复调用；manager 关闭后不能重新启动。

启动会为每个已发布 warm slot 取得 fencing claim，重新连接远端实例，执行数据面健康检查并续期。
缺失实例会在启动返回前被原子替换。启用 `fail_on_startup_warmup_error=True` 后，认证、重连、健康
检查、续期、创建或 State 发布任一步失败都会向外传播，宿主不得报告 ready。

Manager 运行期间会周期续期或替换 warm 实例。后台补充失败不会推翻已经交给当前请求的 owner
backend，但 `check_ready()` 会持续抛出 `OpenSandboxWarmPoolUnavailableError`，直到容量恢复。宿主应把
该方法纳入 readiness 检查。

关闭会等待正在进行的创建、替换、重置和清理安全落定。有限的 `settlement_timeout` 只限制当前调用方等待，不会取消 manager 已经接管的清理任务。超时会抛出 `OpenSandboxSettlementTimeoutError`，稍后可以再次调用 `aclose()` 继续等待。

## 健康检查与替换

`OpenSandboxConfig.health_command` 用于检查数据面是否可用，默认是 `printf ok`。`get()` 发现已有绑定不健康时，会创建替代实例并更新 handle。

持久 State 保存绑定和 fencing 身份，不保存容器文件。Owner Sandbox 过期后，下一次 `get()` 会创建
替代实例；若业务要求远端过期后仍保留 workspace 内容，宿主必须配置 OpenSandbox volume 或 snapshot
策略。

正在执行的操作会继续使用它开始时取得的 backend。替换完成前，旧 backend 不会被提前关闭；替换调用会等旧实例安全退役后才返回。

## 取消安全

创建、健康检查、替换、重置、销毁和关闭开始后，即使发起它的请求被取消，manager 仍会完成必要的资源回收。调用方收到取消不代表远端清理已经结束。

`OpenSandboxClient.destroy()` 会为每个 Sandbox ID 保留一个 task。并发调用方共同等待同一次远端
kill 和本地 close。调用方取消会先等待 settlement，再继续传播取消。kill 已成功时，SDK close
失败只作为清理证据记录，不会误报成远端销毁失败。Client 关闭前会等待所有活跃 destroy task，
再关闭 `ConnectionConfig` 未提供 transport 时由 Client 创建的共享 transport。并发关闭调用会共同
等待同一个受 Client 持有的结算任务；取消等待者不会取消 transport 关闭，关闭任务失败后仍可重试且
不会丢失所有权。调用方显式传入的 transport 始终按借用资源处理，Client 不会关闭它。

如果业务要在取消后立刻给用户响应，可以让清理继续由 manager 持有，并通过监控或状态接口观察最终结果。

## 查看详情

```python
details = await manager.get_details(key)
if details is not None:
    print(details.sandbox_id)
    print(details.available, details.healthy)
    print(details.owner_key, details.cached)
```

`None` 表示这个 key 没有已知绑定。`available=False` 时查看 `unavailable_reason`，它可能是 `not_found` 或 `unreachable`。

下一篇：[受限根目录与文件操作](rooted-filesystem.md)。
