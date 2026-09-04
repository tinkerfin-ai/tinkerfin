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
查询、SQL 和应用层都不判断上游 stream mode 或版本。Profile identity 只属于框架集成与恢复
元数据，不是 Graph 字段或应用查询维度。

## Ledger 与 Graph

每个 Turn 都是一次用户任务的容器，不是 Graph 节点。Turn 顶层作用域内的事件彼此平级；只有
Subagent 拥有嵌套作用域，内部事件仍按真实开始顺序平级排列。作用域可以呈现为：

```text
Turn
├── HumanMessage
├── Context / Memory / Guardrail / retrieval / custom / Plan / interaction
├── Model
├── Tool
├── Subagent
│   ├── HumanMessage
│   ├── Model / Tool / 上下文事件
│   └── AssistantMessage
└── AssistantMessage
```

当前 kind 为 `human_message`、`assistant_message`、`context`、`model`、`tool`、
`subagent`、`memory`、`guardrail`、`retrieval`、`custom`、`plan` 和 `interaction`。
ToolMessage 结果保留在对应的 Tool 事件中。Tool 提议、审批后的真实执行、结果与失败属于同一个
事件；已验证的 Deep Agents `task` 执行只显示为 Subagent，不额外显示 Tool。被 HITL
拒绝的动作不会伪造执行。

`parent_subagent_id` 表示最近的所属 Subagent，也是唯一的展示嵌套关系。`model_call_id` 把
AssistantMessage、Tool 或 Subagent 与产生它的 Model 调用关联起来，但不会把 Model 变成展示父级。
精确依据缺失时通过 link issue 表达，不按时间或到达顺序猜测。非空 namespace 本身不能证明节点
位于 Subagent；只有经过校验的 Subagent 来源才能标记该作用域，普通 LangGraph 子图仍留在 Turn
顶层作用域。

每次 Provider 调用前都会生成一个 Context 事件。它从同一作用域中的上一项可见执行边界开始，
到最终请求真正进入 Provider 时结束；恢复仍在执行的 Subagent 时，以本次 resume 输入重新开始，
不会把人工审批等待时间计入准备耗时。Context 内容从同一 Model 请求中的最终 SystemMessage
投影，不在 Ledger 重复保存正文。请求没有 SystemMessage 时仍保留真实计时的 Context，但内容
为空。该时长表示可观测的墙钟准备延迟，不是 CPU 性能分析。

Middleware 的执行过程不是 Trace 事件，通用 chain 和 middleware callback 不参与采集。业务确需
展示时，middleware 可以通过 `trace_contribution(...)` 显式发布 Memory、Guardrail、retrieval
或 custom 事件。Trace 不定义 Skill kind 或 fact；模型读取 `SKILL.md` 时只产生普通
`read_file` Tool 事件，不生成第二个节点。

经过校验的 Subagent 作用域内，首个 HumanMessage 只投影已采集的 `task.description`；Subagent
的 request 仍保留完整任务参数。两种视图引用同一个 Ledger Fact，不会重复保存正文。

同一作用域先按 `started_seq` 排序；序号相同时依次使用用户、上下文、模型、工具、子智能体、
助手和事件 ID。公共投影使用显式栈进行迭代式深度优先遍历，使每个展开的 Subagent 紧接自己的
内部序列，不依赖 Python 调用栈。

Graph 筛选直接在 Store 中执行。request、result、message 和 state 正文只保存在 Ledger；正文
搜索会先应用有索引的 metadata、namespace 和时间条件，再通过当前 Codec 解码有界候选集，不
保存明文搜索文档。SQL 按节点和 Run revision 存储，因此 sibling branch 互不覆盖；删除消息会
写入当前 lineage 的 tombstone，不会删除其他 Run 分支中的事件。

筛选先确定直接命中，再由 Store 通过有界广度优先查询补齐所属 Subagent 链；补入的容器不进入
`matched_node_ids`。Subagent 最多嵌套 64 层，`max_total_nodes` 限制完整页面，SQL Subagent 和
Ledger locator 每批最多读取 500 个 key。一次 Graph 查询最多选择 10,000 个 lineage Run。

## 历史与实时更新

`Tracer.get()` 返回所选 lineage 的固定前缀 messages、reasoning、state、interactions、summary、
事件页和权威 `TraceThread.graph`；实时更新通过 `TraceUpdate.graph` 携带同一 Graph。存在多个
head 时必须指定 `head_run_id`。

`Tracer.query()` 返回当前权威 Graph。分页 cursor 只绑定准确的当前尾序号和筛选条件；尾序号变化
后旧 cursor 明确失效。只有当前第一页可以调用 `TraceGraphQuery.follow()`，每个增量都携带完整
当前事件顺序与直接命中集合。

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
