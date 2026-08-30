# Tracing API 参考

[语义 Trace](index.md) · [English](../../en/tracing/api-reference.md)

## `Tracer`

```python
Tracer(
    store=None,
    capture_policy=None,
    reasoning_capture_policy=None,
    limits=None,
    write_policy=None,
    projections=(),
)
```

| 成员 | 含义 |
| --- | --- |
| `store` | 借用的 `TraceStore`；默认新建 `InMemoryTraceStore` |
| `open_run(context)` | TinkerFin Runtime 使用的 `RuntimeObserver` 入口 |
| `get(thread_id, head_run_id=None, limit=100, history_cursor=None, projections=())` | 最新或 cursor 固定 as-of 的 `TraceThread` |

同时传入 `store` 与 `limits` 时，两者必须完全相等。Projection 名必须规范、唯一，并在构造时
一次性注册。

## Capture 与容量

| API | 用途 |
| --- | --- |
| `CapturePolicy.public_history(tool_overrides=None)` | 默认策略；保存全部 Tool 经清理的完整内容，并支持按准确名称覆盖 |
| `CapturePolicy.public_safe(tool_rules=())` | Tool 默认仅保留元数据，可通过低层 RFC 6901 规则选择内容 |
| `ToolTraceCapture.full_content()` | 保存 Tool 生命周期、完整安全内容与公开审批说明 |
| `ToolTraceCapture.metadata_only()` | 保存 Tool 生命周期，不保存参数、结果或审批说明 |
| `ToolTraceCapture.selected_content(...)` | 保存 Tool 生命周期与选定的参数/结果路径 |
| `ToolTraceCapture.disabled()` | 不保存 Tool 生命周期及其结果消息 fact |
| `ReasoningCapturePolicy.omitted()` | 默认推理策略，不保存正文或 digest |
| `ReasoningCapturePolicy.content()` | 显式保存 Runtime 已配置 extractor 提供的有界正文 |
| `ToolCaptureRule(toolName=..., argumentPaths=..., resultPaths=..., includeReviewDescription=False)` | 为一个 Tool 放行指定 RFC 6901 值与可选审批说明 |
| `CapturedValue` | 明确的 `inline` 或 `omitted` 捕获 envelope |
| `TraceLimits` | 不可变的 event、thread、Tracer、reserve 与 follow 上限 |
| `TraceWritePolicy` | 不可变的 batch、64 MiB pending、背压与等待策略 |

`TraceLimits` 默认值：

| 字段 | 默认值 |
| --- | ---: |
| `maxEventBytes` | 1 MiB |
| `maxThreadEvents` | 100,000 |
| `maxThreadBytes` | 256 MiB |
| `maxTracerThreads` | 10,000 |
| `maxTracerBytes` | 1 GiB |
| `terminalReserveEventsPerRun` | 4 |
| `terminalReserveBytesPerRun` | 4 MiB |
| `followBatchSize` | 256 |

普通 append 不能占用 active Run 的终态 reserve。Store 的 mandatory append 只接受
`run.terminal` 和 `run.closed` facts。字节 reserve 必须覆盖
`terminalReserveEventsPerRun * maxEventBytes`，保证每个预留 event 都能达到声明的最大值。
已经失败的 session 仍属于不完整 Trace；reserve 不会在 Observer 失去所有权后伪造 fact。
无法容纳完整恢复信息的 interaction payload 会在 durable pause 发布前使 Observation 失败，不能静默
退化为缺少详情的盲审批。

## `TraceThread`

| 成员 | 含义 |
| --- | --- |
| `key` | namespace、thread ID 与不可复用 generation |
| `as_of_seq` | 不可变的全局 Ledger 前缀 |
| `head_run_id` | 当前选择的 branch head |
| `available_heads` | 该前缀下所有可选 head |
| `messages` | 已加载 Turn 窗口中的正序 `TraceMessage` |
| `reasoning` | 已加载 Turn 窗口中显式提取的 `TraceReasoning` |
| `tree` | 扁平权威 `TraceTree`；`roots` 与 `children(id)` 是便利视图 |
| `state` | 所选谱系的完整 root 与无碰撞 subgraph state |
| `interactions` | 窗口内审批、澄清、review 与输入交互 |
| `summary` | 所选谱系的完整累计 status、completeness、计数、pending interaction 与来源最大时间 |
| `status` | `running`、`waiting`、`succeeded`、`failed`、`cancelled`、`abandoned` 或 `unknown` |
| `completeness` | 独立的 missing-prefix、missing-tail、payload-omitted 信号 |
| `message_count` / `tool_call_count` | 从 `summary` 计算的兼容读取视图 |
| `projections` | 显式请求业务 Projection 的防御性结果副本 |
| `has_older` | 是否还有更早完整 Turn |
| `history_cursor` | 供下一次 `Tracer.get(...)` 使用的 generation/head/as-of opaque cursor |
| `load_older(limit=100)` | 扩展历史，不改变 as-of 或 follow 起点 |
| `events(cursor=None, limit=100)` | 升序的所选谱系事件页 |
| `follow()` | 原始 as-of 之后的 `TraceUpdate` 实时迭代器 |
| `delete()` | 删除当前 handle 的确切不活跃 generation |

`TraceUpdate` 包含已提交的所选谱系 event/fact，message、reasoning、node、interaction 的
upsert/remove、当前完整 state 与 `summary`。`update.summary` 是应用该 update 后的累计完整值，
不是 delta；平铺 status、completeness 与计数字段都从同一个值计算。

`TraceSummary.pending_interactions` 与 `last_occurred_at` 覆盖 fixed-as-of 的完整所选谱系，不受
已加载 Turn 窗口影响。Pending 按首次出现的 `trace_seq` 排序，resolved 或 cancelled 后移除。

每个 `TraceMessage`、`TraceReasoning`、`TraceNode` 与 `TraceInteraction` 都公开首次创建该
实体的权威 Ledger `trace_seq`。跨实体类型合并时必须使用该序号，时间戳与 ID 不能替代顺序。
`TraceInteraction.source_id` 保留规范 Native interrupt ID，使协议客户端无需保存 AG-UI 副本
即可按已声明规则生成每个 action 的公开 ID。Tool 审批还通过 `tool_call_ids`
按 action 位置保留 checkpoint 已证明的精确关联；消费方不得按 Tool 名或 node 顺序重建。

`TraceNode.input` 与 `result` 只包含 capture policy 明确保留的值；`inputOmitted` 与
`resultOmitted` 用于区分省略和 JSON null。`Tracer()` 默认使用
`CapturePolicy.public_history()`，新出现的 Tool 会自动保存经清理的完整内容与公开审批说明。
`toolOverrides` 可按准确名称选择 `fullContent`、`metadataOnly`、`selectedContent` 或 `disabled`。
需要显式选择路径时，仍可通过 `CapturePolicy.public_safe()` 使用低层 `ToolCaptureRule` 与空 RFC 6901
根路径。

## 语义 Facts

`TraceSemanticFact` 是严格判别联合：

| Fact | 稳定含义 |
| --- | --- |
| `TurnFact` | 带权威用户消息 ID 的新普通或 branch 任务 |
| `RunFact` | start、input/resume、resume checkpoint、Observer failure、terminal 或 close |
| `MessageFact` | message start/content/completion/reconciliation/removal |
| `ReasoningFact` | 独立 capture policy 下的显式 extractor 正文、reconciliation 与 completion |
| `ToolFact` | Tool start、参数快照、proposal end 或 result |
| `RuntimeTaskFact` | 带 interrupt ID 与结构 payload metadata 的 LangGraph task start/result |
| `StateRevisionFact` | 一个精确 namespace 中变化的非 message state key |
| `InteractionFact` | pending 到 resolved/cancelled 的人工交互 |
| `SubagentFact` | 已校验的非根 Graph scope start 与父 task completion |
| `PlanRevisionFact` | 公开 `tinkerfin_plan` revision 与 status |
| `NativeExtraFact` | 允许的额外 Native mode 结构 metadata |

`TraceEvent` 增加 Store event ID、全局 `traceSeq`、generation 与实际持久字节数。项目协议不包含
schema 或 protocol version 字段。

## 业务 Projection

```python
from pydantic import BaseModel

from tinkerfin_tracing import TraceSemanticFact


class State(BaseModel):
    count: int = 0


class Result(BaseModel):
    count: int


class CountTools:
    name = "count_tools"
    state_type = State
    result_type = Result

    def initial_state(self) -> State:
        return State()

    def apply(self, state: State, fact: TraceSemanticFact) -> State:
        return State(count=state.count + int(fact.kind == "tool"))

    def finish(self, state: State) -> Result:
        return Result(count=state.count)
```

通过 `Tracer(projections=(CountTools(),))` 注册，再用
`tracer.get(..., projections=("count_tools",))` 显式请求。每次 state transition 和结果都会
经过声明的 Pydantic 类型校验。请求的 Projection 失败时抛出 `TraceProjectionFailed`，不改变
Ledger，也不终止 Agent Run。校验后的 state 会按 Run 与 as-of 缓存；子 Run 从最近祖先
checkpoint 派生，不重新 fold 整段前缀。Projection 必须确定且无外部副作用。

## Store 契约

`TraceStore` 提供 namespace、limits、writer 创建、metadata-only generation snapshot、有界
正向/反向事件读取、Projection checkpoint load/CAS、follow 与删除。`TraceWriter` 精确绑定一个
generation 中的一个 Run，支持原子有序 fact batch 与幂等关闭。`StoreThreadSnapshot` 只包含
`asOfSeq`、字节数与 `StoreWriterSnapshot` metadata，不携带 Ledger 前缀。

`InMemoryTraceStore` 为同一 thread 的并发 Run 分配连续全局序号，拒绝重复或 active Run ID，
为每个 active writer 预留终态容量，禁止 active delete，删除时唤醒 follower，并始终返回防御性
副本。

### `SqlAlchemyTraceStore`

```python
SqlAlchemyTraceStore(
    engine,
    namespace="default",
    limits=None,
    options=None,
    codec=None,
)
```

Store 借用 SQLite 或 MySQL 异步 Engine。`setup()` 创建并反射校验唯一当前 Trace Schema，且
从不销毁 Engine。`SqlTraceStoreOptions` 配置数据库时钟 writer lease、heartbeat interval、
follow polling、retry 次数与 retry delay。

| Extra | Engine URL |
| --- | --- |
| `tinkerfin-tracing[sqlite]` | `sqlite+aiosqlite:///...` |
| `tinkerfin-tracing[mysql]` | `mysql+asyncmy://...` |

MySQL writer 使用 `READ COMMITTED` 与行锁，固定读取使用 `REPEATABLE READ`。SQLite writer
使用 `BEGIN IMMEDIATE`，在 Store 自有有界 retry 期间临时关闭 Driver 阻塞式 busy wait，并在
连接归还宿主池前恢复原设置。两个方言都分配 generation 内连续全局序号，执行终态 reserve，
把过期且未完成的 ownership 保留为 missing-tail 证据，支持 fenced takeover，并按有界页轮询
跨实例 follow。

Projection checkpoint CAS 只允许向前推进；唯一例外是以相同 prefix 与 canonical state 重复
提交，用于查证未知提交结果。同一 identity/prefix 出现不同 state 时抛出
`TraceStoreProtocolError`。

### Canonical payload 与 SQL Schema

`CanonicalTracePayloadCodec` 编码 key 排序、无额外空白、finite 的 UTF-8 JSON。
`EncodedTracePayload.digest` 是存储转换前 canonical bytes 的 SHA-256。SQL event 与 Projection
payload 是 opaque bytes；可查询的 Run、fact kind、timestamp、sequence 与 digest metadata 会和
解码内容交叉校验。

`get_trace_store_schema(dialect="sqlite" | "mysql")` 使用与 `setup()` 相同的 metadata，返回
确定性空库 DDL。Trace 精确拥有 namespace、thread、writer、event 与 Projection checkpoint
五张表；表内没有外键或项目自有 Schema-version 字段。

## 错误

Tracing 自有失败都继承 `TracingError`，并使用稳定 `tracing.*` code。公开错误族包含 invalid
cursor、thread not found、ambiguous head、Run not found、Run conflict、corruption、Store
unavailable/timeout/protocol error、Projection checkpoint conflict、quota exceeded、capture rejected、Observer failed 与
Projection failed。公开 `context` 只含客户端安全信息；可信诊断与 cause 独立保存。
