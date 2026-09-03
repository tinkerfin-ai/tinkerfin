# Tracing API 参考

[语义 Trace](index.md) · [English](../../en/tracing/api-reference.md)

## `Tracer`

```python
Tracer(
    store=None,
    capture_policy=None,
    reasoning_capture_policy=None,
    redactor=None,
    graph_query_limits=None,
    limits=None,
    write_policy=None,
    projections=(),
)
```

| 成员 | 含义 |
| --- | --- |
| `store` | 借用的 `TraceStore`；默认新建 `InMemoryTraceStore` |
| `capture_policy` | 正文保留、Tool 选择与错误消息策略 |
| `reasoning_capture_policy` | 独立控制是否保存已提取的 reasoning 正文 |
| `redactor` | 可选的额外业务 `TraceRedactor` |
| `graph_query_limits` | Graph 直接节点、总节点与序列化字节上限 |
| `open_run(context)` | TinkerFin Runtime 使用的 `RuntimeObserver` 入口 |
| `get(thread_id, head_run_id=None, limit=100, history_cursor=None, projections=())` | 固定前缀的会话历史 |
| `query(thread_id, where=None, head_run_id=None, cursor=None, limit=100)` | 当前索引上的 `TraceGraphQuery` |
| `rebuild_graph(thread_id)` | 从 Ledger fact 重建可丢弃的 Graph 索引 |

同时传入 `store` 与 `limits` 时，两者必须与 `store.limits` 完全一致。

## Graph 数据

`TraceGraphFilter` 支持 `kinds`、`statuses`、`parent_id`、`agent_names`、
`middleware_names`、`skill_names`、`providers`、`models`、`namespaces`、`search`、
`started_after`、`started_before`、`include_technical_nodes` 和
`include_ancestor_nodes`。

`search` 按字面子串过滤节点元数据。只含 ASCII 的查询只折叠查询和元数据中的 ASCII
`A-Z`，不会把 Unicode 字符映射为外观相近的 ASCII；查询中只要包含非 ASCII 字符，就按
大小写精确匹配。该定义无需保存重复搜索正文，并保证 SQLite、MySQL 与内存实现返回一致
结果。

`TraceGraphQuery` 提供：

- `snapshot`：完整 `TraceGraphPage` 的防御性副本；
- `turns`、`nodes`、`ordered_node_ids` 和 `root_node_ids`；
- `next_cursor`、`as_of_seq`、`facets` 与 `completeness`；
- `follow()`：只允许当前第一页使用的 `TraceFollow[TraceGraphDelta]`。

`TraceGraphNodeKind` 包含 HumanMessage、AssistantMessage、SystemMessage、ToolMessage、
Agent、Model、Tool、Subagent、Skill、middleware、Memory、Guardrail、retrieval、
custom、Plan、interaction、Run 与 Runtime task。SystemMessage、ToolMessage、
middleware、Run 和 Runtime task 属于技术节点。

`TraceGraphDelta` 提供 Turn 和节点的 upsert/remove，以及当前 `next_cursor`、完整的节点
顺序、根节点、Facet 和 Completeness。带分页 cursor 的查询不能 follow；当前 Ledger 尾序号
变化后，旧 cursor 会明确失效，因此每个实时 Delta 都会原子替换上一 cursor。

`Tracer.get()` 返回的 `TraceThread.graph` 是与 messages、state 相同固定前缀及已加载 Turn
窗口对应的完整 `TraceGraph`。`TraceThread.follow()` 通过 `TraceUpdate.graph` 发布
`TraceGraphDelta`，不再提供另一套树节点模型。当前尾部历史读取可丢弃的 Graph 索引；固定旧
前缀则从 Ledger fact 重放同一个 reducer。

`TraceGraphCompleteness` 分别表达 callback 依据缺失、关系依据缺失和详情被采集或响应上限省略。

## 查询上限

```python
TraceGraphQueryLimits(
    max_direct_nodes=1000,
    max_total_nodes=4000,
    max_page_bytes=8 * 1024 * 1024,
)
```

直接匹配先受节点上限约束，再补齐祖先。响应超过字节预算时，框架先省略 content、request、
result、usage 与响应元数据，同时保留权威结构；仅结构仍超限时抛出 `TraceQuotaExceeded`。

## Capture 策略

| API | 用途 |
| --- | --- |
| `CapturePolicy.public_history(...)` | 默认保存完整且已脱敏的 Tool 正文 |
| `CapturePolicy.public_safe(...)` | Tool 默认只保留元数据，除非显式选择路径 |
| `ToolTraceCapture.full_content()` | 保存 Tool 生命周期、参数、结果和公开审批说明 |
| `ToolTraceCapture.metadata_only()` | 保存生命周期但不保存正文 |
| `ToolTraceCapture.selected_content(...)` | 保存选定的 RFC 6901 路径 |
| `ToolTraceCapture.disabled()` | 不生成该 Tool 的 Trace fact |
| `MiddlewareTraceCapture.visible()` | 保存 callback 能证明的实际执行生命周期 |
| `MiddlewareTraceCapture.disabled()` | 不生成 middleware 专属 fact |
| `ReasoningCapturePolicy.omitted()` | 不保存已提取 reasoning 的正文或 digest |
| `ReasoningCapturePolicy.content()` | 授权保存有界的已提取 reasoning 正文 |

`CapturePolicy` 不再提供直接处理原始值的方法。Runtime 值统一进入 `Tracer` 拥有的强制管道。

## 业务脱敏

```python
class TraceRedactor(Protocol):
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue: ...
```

`RedactionContext.content_kind` 可取 `message`、`model_request`、`model_response`、
`tool_arguments`、`tool_result`、`state`、`interaction`、`plan`
或 `custom`。`component_name` 只包含可选的公开组件名，不提供 thread、Run 或用户身份。

`CompositeRedactor(*redactors)` 按声明顺序传递结果。
`redact_json_paths(value, paths=(...))` 返回独立副本，并把匹配的 RFC 6901 位置替换为
`{"$type": "redacted"}`。

Redactor 必须同步、确定、可重入、无 I/O，且不得修改输入。异常、awaitable、原地修改、非法
JSON、非有限数字或破坏 state、模型消息及 HITL 必需结构时，框架抛出
`TraceCaptureRejected`，不会回退保存原文。

框架在业务链前后都执行凭据与已验证私有 reasoning 清理，最终安全检查不能关闭。

## 存储接口

| 接口 | 职责 |
| --- | --- |
| `TraceStore` | Ledger generation、固定读取、follow、checkpoint 与删除 |
| `TraceGraphStore` | 有界的直接 Graph 查询 |
| `TraceGraphRebuildStore` | 重建可丢弃的 Graph 索引 |
| `TraceLedgerBackend` | 五操作持久 Ledger 边界 |
| `TraceGraphQueryBackend` | 可选的持久 Graph 索引查询 |
| `TraceGraphRebuildBackend` | 可选的持久 Graph 重建 |
| `CanonicalTracePayloadCodec` | 规范编码或可逆加密转换 |

低层 writer 只持久化已经 Capture 的 fact。需要框架和业务强制脱敏时，应使用能够获得来源
上下文的 `Tracer` 路径。
