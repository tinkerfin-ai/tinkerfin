# 受限根目录与文件操作

[Sandbox 生命周期](lifecycle.md) · [English](../../en/sandbox/rooted-filesystem.md)

设置 `workspace_root` 后，Agent 看到的 `/` 会映射到 Sandbox 中指定的物理目录。例如 `/workspace` 配置下，Agent 的 `/src/app.txt` 表示远端 `/workspace/src/app.txt`。

```python
config = OpenSandboxConfig(workspace_root="/workspace")
```

## 为什么使用受限根目录

- 文件工具不能通过 `..` 走出 workspace；
- 指向 workspace 外部的符号链接会被拒绝；
- `reset()` 只清空 workspace 内容，不删除根目录本身；
- Agent 看到的路径更短，也不必知道远端物理目录。

Shell 是单独的能力。原始 Shell 命令不会被文件工具的路径边界自动限制，因此仍要通过工具权限和人工审批控制危险命令。

## 常用异步文件操作

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

### 方法参数

| 方法 | 参数 | 作用 |
| --- | --- | --- |
| `aread()` | `file_path`、`offset=0`、`limit=2000` | 按行读取文本 |
| `awrite()` | `file_path`、`content` | 写入完整文本 |
| `aedit()` | `file_path`、`old_string`、`new_string`、`replace_all=False` | 精确替换文本 |
| `adelete()` | `file_path` | 删除文件或目录，但不能删除虚拟根 |
| `als()` | `path` | 列出目录 |
| `aglob()` | `pattern`、`path=None` | 查找路径 |
| `agrep()` | `pattern`、`path=None`、`glob=None`、`max_count=None` | 搜索文本 |

每个结果对象都可能包含正常数据和错误说明。批量或搜索场景不要只检查列表是否为空，也要检查结果中的 error。

## 执行命令

```python
result = await backend.aexecute(
    "python -m pytest",
    timeout=300,
)
```

`timeout=None` 使用 backend 的默认命令超时。命令从配置的工作目录开始，但 Shell 自身可以访问 Sandbox 内其他路径。

如果命令输出可能很大，可以启用 capture offload：

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

| 参数 | 作用 |
| --- | --- |
| `capture_path` | 大输出在虚拟根中的保存位置 |
| `max_inline_bytes` | 直接放在结果中的最大 bytes |
| `max_capture_bytes` | 可选的完整捕获上限 |
| `timeout` | 命令超时秒数 |

## 上传和下载

```python
uploads = await backend.aupload_files([("/input/data.csv", csv_bytes)])
downloads = await backend.adownload_files(["/output/report.json"])
```

输入顺序和响应顺序一致。某个明确无效的路径只影响对应项；网络失败或结果不确定时会直接抛出异常，不会自动重放可能已经完成的写操作。

受限上传和下载要求 Sandbox 镜像提供 Python 3、Linux procfs，并允许命令服务与文件服务共享进程视图。默认 TinkerFin Sandbox 镜像满足该要求。

## 接入 AgentRuntime

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

Runtime 在运行开始时一起准备 rooted backend 和文件 middleware。权限规则需要 interrupt 而非 deny 时，必须配置 checkpointer。只有由调用方自行管理的 Deep Agents Graph 才需要直接使用 `build_rooted_filesystem_middleware()`。

下一篇：[多进程持久化与自定义扩展](persistence-and-extensions.md)。
