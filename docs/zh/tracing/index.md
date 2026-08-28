# 语义 Trace

[文档首页](../README.md) · [English](../../en/tracing/index.md)

`tinkerfin-tracing` 只从两个权威来源记录用户可读执行历史：

```text
TinkerFin Runtime 生命周期
+ 校验后的 LangGraph Native messages/tasks/values
```

它不记录 AG-UI event、Messaging commit/settlement、SSE frame、Redis lease 或投递所有权。
这些信号描述展示与传输，不等同于 Agent 做过什么。

## 安装

```bash
pip install tinkerfin tinkerfin-tracing

# 需要持久 SQL Store 时选择一个方言
pip install "tinkerfin-tracing[sqlite]"
pip install "tinkerfin-tracing[mysql]"
```

`tinkerfin-tracing` 自身只依赖 `tinkerfin-contracts` 与 Pydantic。仅 import 该包不会加载
Deep Agents、LangGraph、AG-UI、Messaging、SQL、Redis、Sandbox 或宿主应用。SQLAlchemy 与
Driver 只存在于可选 extra，请求 SQL 公共符号时才加载。

## 记录 Runtime

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_tracing import Tracer


tracer = Tracer()
agent = (
    TinkerFin()
    .observe(tracer)
    .create_deep_agent(
        model="openai:gpt-5.4",
        tools=[],
    )
)

runtime = agent.new(
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
)
async for part in runtime.astream(graph_input):
    consume(part)

thread = await tracer.get("thread-1")
print(thread.messages)
print(thread.tree.roots)
print(thread.state.root)
print(thread.interactions)
print(thread.status.execution)
```

`.observe(...)` 返回单独配置的 `TinkerFin` factory。每个请求打开一个 request-scoped Trace
session。Runtime 会先校验每个 Native part，再写 Trace，之后才调用 `on_part` 和 AG-UI 转换；
在 resume、interrupt、terminal 和 close 边界强制结算已接收 Observation。Trace 写入失败会让
Agent Run fail-closed。

## 读取固定执行切面

`await tracer.get(thread_id)` 先固定一个不可变的全局 `as_of_seq`。默认历史窗口包含最新 100
个完整 Turn：

- `messages`、`tree`、`interactions` 使用 Turn 窗口；
- `state`、`status`、`completeness` 覆盖所选谱系在固定 as-of 的完整事实；
- `await thread.load_older(limit=100)` 只扩展历史窗口；
- 消息保持正序，顶层 Turn 节点按最新优先。

普通输入或带新用户输入的 branch 会创建 Turn。resume、审批、澄清和 Plan review 只在同一
Turn 中增加 Run segment。

同一 thread 有多个 branch head 时，`get()` 抛出 `AmbiguousTraceHead` 并给出所有可选 Run
ID。需要显式选择：

```python
thread = await tracer.get("thread-1", head_run_id="run-branch-a")
```

## 分页与实时跟随

```python
page = await thread.events(limit=100)
while page.next_cursor is not None:
    page = await thread.events(cursor=page.next_cursor, limit=100)

async for update in thread.follow():
    apply_message_delta(update.messages)
    apply_node_delta(update.nodes)
```

事件 cursor 是不透明值，绑定 namespace、thread、generation、selected head、固定分页 as-of
与最后全局序号。并发 append 不会进入已经开始的分页。`follow()` 从 handle 原始 as-of 之后
开始，返回语义实体的 upsert/remove；取消或关闭迭代器会释放等待资源。

`thread.delete()` 只删除该 handle 指向的不活跃 generation。存在 active writer 时拒绝删除；
旧 generation 的 cursor 或 handle 不能访问同 ID 重建后的 thread。

## 公开安全捕获

`CapturePolicy.public_safe()` 保留公开用户与助手正文，同时独立执行以下安全规则：

- 删除 provider-private `additional_kwargs.reasoning_content`，但不全局删除其他位置的同名
  业务字段；
- 精确名称和带供应商前缀的凭据字段写为 redacted；
- Runtime 声明的顶层 private state channel 在 state、task 与额外 mode 结构 fact 进入 Trace
  前删除；
- Run 与 Runtime task payload 只保留有界结构元数据，不复制 state 或 message 正文；
- Tool 参数、结果和审批参数默认 metadata-only，只有按 Tool 名和 JSON Pointer 显式
  allowlist 的值可以保存；
- 安全化后仍超限的 payload 使用明确 omitted disposition，不做局部或静默截断。
- non-finite 数字在 JSON 序列化前直接拒绝。

Provider reasoning 需要两道彼此独立的显式授权：先在 `DeepAgentsV2RuntimeProfile` 配置已验证的
extractor，再仅在允许持久化时向 `Tracer` 传入 `ReasoningCapturePolicy.content()`。默认
`ReasoningCapturePolicy.omitted()` 只记录明确 omission，不保存正文或 digest。启用 AG-UI
reasoning event 不会授权 Trace 持久化；保留路径之外名为 `reasoning_content` 的业务字段仍是
普通业务数据。

Trace view 返回防御性副本。修改返回字典或 event 不会改写 Ledger 或后续查询。

## Store 边界

`Tracer()` 默认使用 `InMemoryTraceStore`。它有容量上限、全异步、仅当前进程可见，进程退出后
数据丢失。

`SqlAlchemyTraceStore` 使用同一 `TraceStore`/`TraceWriter` 契约提供 SQLite 与 MySQL 8.x
持久化。它借用宿主管理的 `AsyncEngine`，不修改连接池大小，也不销毁 Engine。应用应在启动时
调用 `await store.setup()`。setup 可幂等并发执行，只创建 Trace 自有表，并在接收请求前反射校验
当前列、类型、nullability、主键、索引、comment 以及无外键约束。

```python
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer

engine = create_async_engine("mysql+asyncmy://user:password@db/tinkerfin")
store = SqlAlchemyTraceStore(engine, namespace="my-application")
await store.setup()
tracer = Tracer(store=store)
```

SQL writer 使用数据库时钟 lease 与单调递增 fence。过期且未完成的 writer 会表现为
`missing_tail`，继续保留终态容量并允许 takeover；已完成 Run 不允许 takeover。SQLite 锁等待
进入有界且可取消的 Store retry；MySQL 对锁超时、死锁和未知提交结果重试，并用保留的 event ID
或 checkpoint digest 查证。固定读取使用同一个 repeatable database snapshot。

Fact 与 Projection state 使用 canonical finite UTF-8 JSON，SHA-256 digest 覆盖存储转换前的
canonical bytes，并在读取时校验。Trace 持久化不包含 S3/Blob 归档、加密/KMS 或
OpenTelemetry exporter。具体 Archive/Blob 集成必须实现或装饰 `TraceStore`；加密包装 canonical
payload codec 且不改变转换前 digest；telemetry 通过 `RuntimeObserver` 或 Store/Messaging Backend
decorator 观察。只有这些已经实际使用的边界属于扩展合同，Tracing 不提供空能力接口。

下一篇：[Tracing API 参考](api-reference.md)。
