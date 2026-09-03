# 语义 Trace

[API 参考](api-reference.md) · [English](../../en/tracing/index.md)

TinkerFin Tracing 把规范化 Runtime Observation 写入一个只追加的语义 Ledger，并生成一个可重建
的执行 Graph。Ledger 是唯一权威；Graph 行和 Projection checkpoint 都是派生索引。

## 托管执行

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_tracing import Tracer

tracer = Tracer()
tinkerfin = TinkerFin().observe(tracer)
agent = tinkerfin.create_deep_agent(model="openai:gpt-5.4", tools=[])

result = await tinkerfin.ainvoke(
    RunIdentity(threadId="thread-1", runId="run-1"),
    agent=agent,
    input=graph_input,
)
```

托管 invoke 和 stream 会在模型输出前打开 Runtime Observation，因此供应商尚未产生 token 时，
Run 和 HumanMessage 已经可以查询。直接调用编译后 graph 属于不受托管的高级入口。

稳定 v2 与实验性 v3 Runtime Profile 会把不同上游流转换为相同 Observation。Trace fact、Graph
查询、SQL 和应用层都不判断上游 stream mode 或版本。

## Ledger 与 Graph

每轮会话以 HumanMessage 为根：

```text
HumanMessage
└── Run / Agent
    ├── Model
    │   ├── SystemMessage
    │   └── AssistantMessage
    └── Tool
        ├── ToolMessage
        ├── Skill
        └── Subagent
```

实际结构以现有依据为准。父级、模型输出 ID、Tool 提议或执行依据缺失时，框架返回 link issue，
不会按时间或到达顺序关联并行工作。Tool 提议、审批后的真实执行和结果会聚合为一个 Tool 节点；
被 HITL 拒绝的动作不会伪造执行。

SystemMessage、ToolMessage、middleware、Run 和 Runtime task 属于技术节点，默认不返回。隐藏
技术节点时，框架会把每个可见节点重连到最近的可见祖先，再返回权威顺序和根节点。

Graph 筛选直接在 Store 中执行。request、result、message 和 state 正文只保存在 Ledger，只有
命中节点才会按序号读取。SQL 按节点和 Run revision 存储，因此 sibling branch 互不覆盖；删除
消息会写入当前 lineage 的 tombstone，不会删除祖先或 sibling 节点。

## 历史与实时更新

`Tracer.get()` 返回所选 lineage 的固定前缀 messages、reasoning、state、interactions、summary、
事件页和权威 `TraceThread.graph`；实时更新通过 `TraceUpdate.graph` 携带同一 Graph。存在多个
head 时必须指定 `head_run_id`。

`Tracer.query()` 返回当前权威 Graph。分页 cursor 只绑定准确的当前尾序号和筛选条件；尾序号变化
后旧 cursor 明确失效。只有当前第一页可以调用 `TraceGraphQuery.follow()`，每个增量都携带完整
当前顺序和根节点。

每个 follow handle 都拥有并关闭上游 Store iterator。正常结束、异常、取消、重复取消和消费方
提前退出遵守相同的资源所有权与背压规则。

## 安全

Tracer 始终按以下顺序处理：

- 把来源值转换为有限的标准 JSON；
- 清理精确名称及供应商前缀的凭据字段；
- 只删除已验证的 provider-private `additional_kwargs.reasoning_content` 路径；
- 删除 Runtime 声明的顶层私有 state channel；
- 执行可选的业务 Redactor；
- 再次执行强制凭据和私有 reasoning 清理；
- 应用保留策略与字节上限，然后创建 Fact。

保留路径之外同名的业务 `reasoning_content` 不会被删除。业务 Redactor 只接收独立 JSON 和功能化
`RedactionContext`，不会拿到 LangChain 对象或执行身份。扩展失败时拒绝 Capture，不回退保存原文。

Provider reasoning 默认不保存；只有 Runtime 配置已验证 extractor，并且 Tracer 显式使用
`ReasoningCapturePolicy.content()` 时才会保留有界正文。

## 持久化

`InMemoryTraceStore` 有容量上限且只在当前进程有效。`SqlAlchemyTraceStore` 通过借用的异步
Engine 支持 SQLite 与 MySQL。应用应在接收请求前调用 `setup()`，让陈旧或不完整 Schema 提前
失败。

Store 只拥有 namespace、thread、writer、Ledger event、Projection checkpoint 和 Graph node
六张表。查询哈希键使用 32-byte SHA-256 二进制值；Graph 表除主键外只维护一个 lineage 索引。

自定义持久化实现 `TraceLedgerBackend`，Graph 查询和重建使用独立的可选协议。Codec 可加密规范
字节；Runtime Observer 或 Backend decorator 可导出遥测，不需要改变语义 fact。

当前不提供 Archive/S3/Blob 实现或 OpenTelemetry exporter。高级集成可以实现
`TraceLedgerBackend`、替换 `TraceStore`、包装 canonical codec、观察 `RuntimeObserver` 或装饰
Store 操作。Messaging Backend 扩展仍属于独立投递边界，不持久化 Trace fact。
