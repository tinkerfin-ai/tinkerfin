# TinkerFin Studio 后端

## 是什么

该应用提供认证、数据库模型目录、Deep Agents 会话、AG-UI SSE、HITL、Trace 历史、
远程取消和用户级 OpenSandbox。Runtime 与已校验 Native 事实写入 MySQL Trace Store；
AG-UI 帧只在 Redis Messaging 的有限重播窗口内保存，不作为长期会话正文。

本目录只部署 Studio 后端，不包含 Web、反向代理、TLS、HA、数据库版本迁移、备份
或回滚系统。单机 Compose 适合可信主机；OpenSandbox 服务会挂载 Docker socket，
因此拥有等同宿主机 Docker 管理员的能力。

## 本地安装

从仓库根目录执行：

```bash
uv sync --all-packages --group dev --locked
cp apps/studio/server/.env.example \
  apps/studio/server/.env
uv run python -m tinkerfin_studio --host 127.0.0.1 --port 8090 --reload
```

进程收到关闭信号后默认等待现有连接 10 秒，再取消仍在运行的 SSE 请求并完成资源清理。
可通过 `--graceful-shutdown-timeout-seconds` 调整等待时间。

本地进程使用应用目录的 `.env`，其中 MySQL、Redis Control、Redis Runtime 和 OpenSandbox
地址必须能从宿主机访问，数据库应先由部署流程自动准备。Control 只承载认证与 Run 协调，
Runtime 只承载 Checkpointer 与 Messaging；两者必须配置为不同物理服务地址。

## 认证会话

`AUTH_TOKEN_EXPIRE_SECONDS` 控制访问令牌从签发时刻起的固定有效期，默认值为 `86400`。
请求和用户操作不会延长到期时间。`POST /api/auth/login` 返回访问令牌、UTC `expires_at`
和用户信息；`GET /api/auth/me` 返回同一 `expires_at` 和当前用户。后端在每个认证请求上以
Redis Control 记录及其固定到期时间为准，过期、撤销或无效令牌统一返回 401。

登录、`/api/auth/me` 和用户查询响应中的 `avatar_url` 为可空 HTTPS 头像地址。
`PATCH /api/user/me` 修改当前登录用户资料：`display_name` 必须是 1～128 个字符，
`avatar_url` 最大 2048 个字符且必须使用 HTTPS，传入 `null` 可清空头像。

配置只作用于服务重新加载后签发的令牌，已有令牌保持签发时确定的到期时间。

## 对话请求边界

`POST /api/conversation/chat` 接收标准 AG-UI `RunAgentInput`，并返回可回放的 AG-UI SSE。HTTP 层先完成协议校验，再由 `ChatRequest.from_agui()` 增加 Studio 的模型、会话归属、请求模式和恢复校验；Service 不接收未经转换的请求。

| 字段 | Studio 的处理方式 |
| --- | --- |
| `threadId` | 空字符串表示新会话；已有值必须属于当前用户 |
| `runId` | 新输入使用新值；同一次网络重试或附着复用原值 |
| `parentRunId` | 保留在标准请求快照中，不作为框架 RunIdentity 或子 Agent 关系 |
| `state` | 保留在标准请求快照中，不自动成为 Graph 输入 |
| `messages` | 普通运行只接受一条非空文本 user 消息；resume 必须为空 |
| `tools` | 仅保留客户端工具描述，不授予服务端工具执行权限 |
| `context` | 保留在标准请求快照中，由业务决定是否使用 |
| `forwardedProps` | `model` 与 `command.plan` 必填；Plan 只接受 `on/off`，未知 command 与其他扩展字段完整保留 |
| `resume` | Studio 只原子认领公开 ID；框架从 Checkpointer 读取真实 pending interrupt，并校验完整覆盖、决策与 Tool correlation |

每条标准 AG-UI 消息必须带非空客户端 ID。该 ID 只用于通过 HTTP 协议校验，不参与权限、幂等、
Graph 关联或持久化身份；Service 会为 Graph 输入与 Trace 快照分配权威消息 ID。
`RUN_STARTED` 只返回 canonical `threadId`、`runId` 和标题，不携带 input。

请求边界不接受 `forwardedProps.mode`。`command.plan=on` 在 run 准备阶段转换为内部
`AgentMode.plan`，`command.plan=off` 转换为 `AgentMode.default`；Trace state 和框架
`open_agui_run(mode=...)` 使用内部模式，不把该字段重新暴露到 HTTP 契约。command 对象允许
保留未来命令字段，但当前服务端只解释 `plan`。

Studio 在应用启动时选定默认稳定 Runtime Profile，不按模型或 Run 选择底层流实现。Profile 只
负责框架与上游实现的集成及 checkpoint 恢复一致性；`profile_id` 不进入 Studio 业务表、HTTP
请求或响应、Trace Graph 查询。Run 注册只固定模型和请求快照，同 Run 重试、branch 和 resume
在建图前校验模型一致性。供应商私有思考字段默认不解析；产品确需展示思考过程时，由宿主在启动
装配处提供能够验证 provider 与消息结构的 `ReasoningExtractor`，不能写入模型配置、会话数据或
客户端协议。框架不提供供应商专用 extractor。

## 会话数据边界

- `GET /api/conversation/history` 只读取 Studio 列表摘要
- `GET /api/conversation/{threadId}/history` 在校验用户归属后返回固定 `asOfSeq` 的 Trace 视图；
  `historyCursor` 只扩展同一固定前缀的 Turn 窗口；`includeTaskTrace=true` 会从同一 Trace 前缀
  查询重建根 Agent 任务轨迹，`false` 跳过该投影
- `GET /api/conversation/{threadId}/trace` 先发送完整 Trace snapshot，再按提交顺序发送语义增量；
  `includeTaskTrace=true` 时只在任务轨迹实际变化后发送完整 replacement；断连或取消会关闭
  底层 follow iterator 与请求内 projector
- Trace 视图和增量携带 `generation`、`asOfSeq`、`observedAt`。同一代按事件序号和存储 UTC
  观测时间排序，保留微秒；writer 失活或有效接管可以在同一序号更新运行状态。列表摘要用相同
  规则拒绝迟到快照；相同观测发生内容冲突时重新读取 Trace。历史分页保留固定前缀的原始观测，
  补充历史内容时保留前端已收到的较新运行状态
- `GET /api/conversation/{threadId}/trace/graph` 在校验用户归属后由 Trace Store 直接筛选链路
  节点，支持 `kind`、`status`、`modelCallId`、`agent`、`provider`、`model`、`namespace`、
  `query`、`startedAfter`、`startedBefore`、opaque `cursor` 与 `limit`；响应中的 Turn 是容器，
  `matchedNodeIds` 只包含直接命中，Store 会补入直接命中所属的 Subagent 链
- `GET /api/conversation/{threadId}/trace/graph/follow` 先发送同一筛选首页，再持续发送节点
  和 Turn 的 upsert/remove、完整 `orderedNodeIds`、`matchedNodeIds`、`nextCursor`、`asOfSeq`
  与 `completeness`；断连或取消会关闭底层过滤跟随器
- `POST /api/conversation/chat` 的当前 owned Run 使用 AG-UI + Messaging；终态会话正文仍以 Trace 为准

Trace Graph 中，同一 Turn 作用域的节点按真实开始序号平级排列，只有 Subagent 形成嵌套。
`parentSubagentId` 是唯一展示嵌套关系；`modelCallId` 只关联 Assistant、Tool、Subagent 与产生它的
Model。Assistant 没有可见正文但对应 Model 确实发出 Tool 调用时，`toolCallOnly` 为 `true`，且不受
API 查询是否返回 Tool 节点影响。非空 `namespace` 只表示 Graph 作用域，必须有经过校验的 Subagent 来源才能形成嵌套。
公共顺序使用显式栈进行迭代式深度优先遍历；Store 通过受 `max_total_nodes` 约束的有界广度优先
查询补齐所属 Subagent，最多接受 64 层，SQL key 每批最多读取 500 个。

Studio 把 Graph kind 固定映射为六类：

| Graph kind | Studio 分类 |
| --- | --- |
| `human_message` | 用户 |
| `context`、`memory`、`guardrail`、`retrieval`、`custom`、`plan`、`interaction` | 上下文 |
| `model` | 模型 |
| `tool` | 工具 |
| `subagent` | 子智能体 |
| `assistant_message` | 助手 |

链路页面始终读取完整六类，不提供节点类型筛选；节点和内容搜索通过 `query` 直接交给 Store。
Subagent 内的首个“用户”只显示已采集的 `task.description`，Subagent 详情仍显示完整任务参数；
两种视图共用同一个 Ledger Fact，不重复入库。

每次模型调用前都有一个“上下文”节点：开始时间取同一作用域中的上一项可见执行边界，结束时间与
模型节点开始时间一致。恢复仍在执行的子智能体时从本次恢复输入重新计时，不包含人工等待时间。
详情中的最终 SystemMessage 直接从同一条模型请求投影，不重复写入 Trace Ledger；请求没有
SystemMessage 时仍显示准备耗时，并明确内容不可用。该时长是墙钟准备延迟，不是 CPU 耗时。

Middleware 正常参与 Agent 执行，但通用 chain 和 middleware callback 不进入 Trace。业务需要可见
上下文时应显式使用 `trace_contribution(...)`。Trace 不定义 Skill 节点；模型读取 `SKILL.md`
只显示为普通 `read_file` Tool。

`database/mysql/schema.sql` 只定义 Studio 拥有的用户、模型、会话归属、Run 注册、
interrupt claim 和列表摘要表，不复制框架表结构。Trace 表由
`SqlAlchemyTraceStore.setup()` 创建并校验；LangGraph Store 在进入资源上下文时初始化；
OpenSandbox State 在 Manager 启动时初始化。Studio 只装配这些公开入口。
链路 Graph 表只保存筛选、关系、状态、时间与 Ledger 序号，不保存 request、result、message
或 state payload；关系字段只包含所属 Subagent 和产生事件的 Model call，详情仍经 Trace codec
读取 Ledger。任务轨迹不写第二份副本、不注册 Projection checkpoint，只读取公共
`TraceThread.events()`。
LangGraph Store 由 `tinkerfin-langgraph-mysql` 通过 asyncmy 管理一条独立连接，进入资源上下文时
自动执行当前 Store DDL，退出、异常或取消时自动关闭。

## 快速部署

要求 Bash、OpenSSL、uv、Docker 和 Docker Compose 2.24 或更高版本。在仓库根目录
执行：

```bash
./apps/studio/server/deploy/setup.sh
./apps/studio/server/deploy/deploy.sh
```

`setup.sh` 创建部署专用 `.env` 和权限为 `0600` 的文件型 Secrets。默认部署启动
Studio、MySQL、Redis Control、Redis Runtime 和 OpenSandbox，只向宿主机回环地址发布 Studio
端口。内置 MySQL 仅在全新数据卷首次启动时导入 Studio 业务 SQL。两个 Redis 使用独立 Secret、
AOF 卷与健康检查。
OpenSandbox 的 SQLite Store 与 Docker runtime metadata 分别使用持久卷；后者保留运行中 Sandbox
续期后的过期时间，使 OpenSandbox Server 容器重建后不会退回创建时的旧时间。

## 外部依赖模式

编辑 `deploy/.env` 与 `deploy/secrets/`，把数据库 URL、两个 Redis 地址和 OpenSandbox 地址
改为容器可达的外部服务，然后执行：

```bash
./apps/studio/server/deploy/deploy.sh --external
```

## 运行约束

- 用户级 Sandbox 工作区使用手动清理生命周期，不会因闲置到期；同一用户的会话共用该工作区，
  持久绑定支持后端正常重启后重连。实例会持续占用资源，需通过明确的清理操作结束使用。
  该策略不提供文件备份，外部删除、宿主机或存储故障仍可能造成文件丢失
- `/health/live` 只表示进程存活
- `/health/ready` 独立检查 MySQL、Redis Control、Redis Runtime、OpenSandbox 控制面与真实预热容量，
  就绪失败返回 503
- `DATABASE_CONNECTION_BUDGET` 必须覆盖共享 SQLAlchemy 池和一条 Agent Store connection；
  启动时还会用 `@@max_connections` 校验 `DATABASE_MANAGEMENT_CONNECTION_RESERVE`
- `MESSAGING_RETENTION_SECONDS` 默认 `86400`，只定义终态后的网络重播窗口；`0` 关闭自动过期
- 容器日志默认只写 stdout；设置 `LOG_FILE_ENABLED=true` 后写入持久日志卷
- 沙箱生命周期回调记录事件、原因、用户绑定身份和工作区变化提示，沿用上述日志输出及 `LOG_LEVEL`。
  不可用和预热容量不足为 WARNING，恢复失败为 ERROR，恢复、重建、重置、销毁及预热恢复等为 INFO。
  用户实例故障在实际访问或检查发现后记录，不保证容器停止时立即通知；进程内回调也不提供可靠审计交付
- `DATABASE_ECHO` 独立于 `LOG_LEVEL` 控制 SQL 语句与参数的日志输出；SQL 日志使用应用日志格式，
  写入标准输出和已启用的文件日志，每个输出各记录一次
- `dist/` 保存本次发布依赖与 wheel；部署通过仓库统一入口在临时副本中构建，并逐字节核对源码、类型声明和包资源；工作树中的 `build/` 与 `.egg-info` 不参与构建
- `docker compose down -v` 会永久删除 MySQL、两个 Redis、OpenSandbox 和日志卷数据
- 文件型 Secrets 优先于普通环境变量；缺失、不可读或内容为空会阻止启动

## 验证

```bash
uv run pytest -q apps/studio/server/tests
uv run ruff check apps/studio/server
uv run pyright apps/studio/server/src apps/studio/server/tests
```

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。
