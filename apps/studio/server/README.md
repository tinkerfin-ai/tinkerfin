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

## HTTP 响应

`/api` 下普通业务 JSON 接口成功时统一返回 HTTP 200 和
`{ "code": 0, "message": "success", "data": ... }`。保存、删除和登出等不返回业务数据的操作使用 `data: null`。
错误使用相同包络并保留对应的 HTTP 状态码，客户端应同时读取 HTTP 状态和业务 `code`。
附件原件与预览返回文件内容，会话与 Trace 订阅返回原生 SSE；`/health/live` 和 `/health/ready` 使用独立的健康检查响应。

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
| `messages` | 普通运行接受一条文字及附件 user 消息，也可仅附件；resume 必须为空 |
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

## 个人模型与附件

“设置 → 模型配置”通过 `/api/models/configurations` 管理当前用户的对话模型和 OpenAI 兼容生图服务；`GET /api/models` 只返回本人启用的对话模型。配置响应不包含密钥，更新时留空保留本人密钥。模型与默认选择按用户和用途隔离。生图只调用明确设置且启用的默认模型，没有默认项时提示完成配置。

`PUT /api/models/configurations/{model_id}/default` 不接收请求体，只启用目标模型并切换本人同一用途的默认选择。目标必须配置密钥；该操作不覆盖连接、密钥或生成参数，也不影响正在运行的会话。

`POST /api/models/configurations/test` 接收 `{"kind":"basic","configuration":{...}}`；`configuration` 使用保存接口相同的配置字段，测试不会保存配置。`kind` 可选 `basic`、`text`、`vision`、`image`，总超时分别为 10、30、45、120 秒。已有密钥仅可在本人同一配置 ID、同一 Base URL 下复用，改地址必须提供密钥。聊天与测试共用 OpenAI 兼容和 DeepSeek 的模型构建入口及地址访问规则。

测试结果位于 `ApiResponse.data`，包含 `kind`、`outcome`（`success`、`failed`、`inconclusive`）、`elapsed_ms`、`code`、可空的 `text` 与 `image`。图片包含 `mime_type` 和 `data_base64`；视觉测试返回测试图和实际回复，生图测试返回经校验、限额的预览。基础检查无法取得模型列表时返回未确认，不据此否定模型能力。供应商错误转为安全错误码，不返回密钥或供应商原始错误正文。测试不自动重试、不修改能力标记、不创建会话附件，能力调用可能产生供应商费用。

`generation_options` 是最多 64 KiB、64 层容器嵌套的严格 JSON 对象，数值必须有限；顶层不能覆盖 `model`、`prompt`、`n`、`api_key` 或 `authorization`（忽略大小写）。`size` 和 `output_format` 如提供则必须为非空字符串，其他供应商字段保留并交给生图接口。

公网模型默认使用 HTTPS。接入 Ollama 或内网模型时，管理员在后端环境配置中设置 `MODEL_ALLOWED_ORIGINS`，列出允许访问的准确协议、主机与端口：

```dotenv
MODEL_ALLOWED_ORIGINS=["http://127.0.0.1:11434","http://localhost:11434"]
```

重新启动后端后，在模型配置中选择 OpenAI 兼容接口，Base URL 填写 `http://127.0.0.1:11434/v1`，Model ID 填写 Ollama 已安装的模型名称；未启用认证的 Ollama 可以填写占位 API Key `ollama`。图片输入和工具调用取决于所选模型的实际能力。

允许列表不包含 `/v1`、查询参数、账户信息或通配符。HTTP、本机及内网访问按协议、主机和端口精确匹配，不因域名指向同一 IP 而自动互相授权。该设置适用于所有用户的聊天、生图和生成图片下载；允许的 HTTP 服务应位于受信任网络。更改设置后需重启后端。

本机地址指 Studio 后端所在的网络空间。Docker 部署访问宿主机 Ollama 时，在 `deploy/.env` 中填写 `MODEL_ALLOWED_ORIGINS=["http://host.docker.internal:11434"]`，模型 Base URL 使用 `http://host.docker.internal:11434/v1`；Ollama 需监听容器可达的地址。连接另一台服务器时，使用该服务器可达的主机名或 IP 并添加对应来源。

附件使用 `POST /api/attachments?name=...` 上传原始文件流，`GET /api/attachments/{id}/content` 读取原件，`variant=preview` 获取图片预览，`DELETE /api/attachments/{id}` 删除本人未发送草稿。图片及文档输入使用 AG-UI 内容块，`source.value` 为 `attachment:<id>`；后端以仓储信息重建 metadata 并在运行登记事务内绑定会话。消息文本仍受 UTF-8 大小限制。

设置 `ATTACHMENT_DIRECTORY` 保存原件与派生图。相对路径以所读取 `.env` 文件所在目录为基准；未指定配置文件时，以应用默认配置目录为基准，默认值为该目录下的 `.data/attachments`。从不同工作目录启动不会改变存储位置。已有附件应配置其实际所在目录；容器使用 `/app/attachments` 数据卷绝对路径。上传总并发与解析并发均有界，文档解析运行在可取消、最长 30 秒的子进程中。未发送附件保留至少 24 小时，上传及启动时回收过期草稿和已删除会话的对象。相关格式、模型配置与工具用法见根目录 [MultiModal.md](../../../MultiModal.md)。

Studio 使用框架默认的 Sandbox Runtime 镜像，预装 Playwright 和无界面 Chromium，可直接执行网页截图。镜像更新只影响随后创建的容器，已有用户工作区不会自动重建。生图使用用户配置，供应商 URL 或 Base64 结果均在保存后才对外发布。

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
Compose 使用固定摘要的官方 `opensandbox/server:v0.2.3`，Studio 的 Python SDK 依赖为
`opensandbox==0.1.16`。该服务的 Docker 恢复操作用于解除暂停，不能启动已通过 Docker 停止的容器。
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
