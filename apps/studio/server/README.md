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
`new_agui(mode=...)` 使用内部模式，不把该字段重新暴露到 HTTP 契约。command 对象允许
保留未来命令字段，但当前服务端只解释 `plan`。

模型目录中的 `runtime_profile` 为必填值。当前 Worker 只注册 `deepagents-v2`；Run 注册会固定
模型与 Profile，同 Run 重试、branch 和 resume 在建图前校验一致性，客户端不能覆盖恢复 Profile。

## 会话数据边界

- `GET /api/conversation/history` 只读取 Studio 列表摘要
- `GET /api/conversation/{threadId}/history` 在校验用户归属后返回固定 `asOfSeq` 的 Trace 视图；
  `historyCursor` 只扩展同一固定前缀的 Turn 窗口
- `GET /api/conversation/{threadId}/trace` 先发送完整 Trace snapshot，再按提交顺序发送语义增量；
  断连或取消会关闭底层 follow iterator
- `POST /api/conversation/chat` 的当前 owned Run 使用 AG-UI + Messaging；终态会话正文仍以 Trace 为准

Studio MySQL 只保存会话归属、Run 注册、interrupt claim 和列表摘要。Trace 的五张表由
`SqlAlchemyTraceStore.setup()` 自动创建并校验，`database/mysql/schema.sql` 同时提供完整空库 DDL。
LangGraph Store 由 `tinkerfin-langgraph-mysql` 通过 asyncmy 管理一条独立连接，进入资源上下文时
自动执行当前 Store DDL，退出、异常或取消时自动关闭。

## 单机部署

要求 Bash、OpenSSL、uv、Docker 和 Docker Compose 2.24 或更高版本。在仓库根目录
执行：

```bash
./apps/studio/server/deploy/setup.sh
./apps/studio/server/deploy/deploy.sh
```

`setup.sh` 创建部署专用 `.env` 和权限为 `0600` 的文件型 Secrets。默认部署启动
Studio、MySQL、Redis Control、Redis Runtime、OpenSandbox 和一次性数据库初始化服务，只向
宿主机回环地址发布 Studio 端口。两个 Redis 使用独立 Secret、AOF 卷与健康检查。

## 外部依赖模式

编辑 `deploy/.env` 与 `deploy/secrets/`，把数据库 URL、两个 Redis 地址和 OpenSandbox 地址
改为容器可达的外部服务，然后执行：

```bash
./apps/studio/server/deploy/deploy.sh --external
```

部署命令会自动初始化空数据库。应用只接受当前完整 Schema，不包含运行时兼容分支或迁移链。

## 运行约束

- `/health/live` 只表示进程存活
- `/health/ready` 独立检查 MySQL、Redis Control、Redis Runtime 与 OpenSandbox，就绪失败返回 503
- `DATABASE_CONNECTION_BUDGET` 必须覆盖共享 SQLAlchemy 池和一条 Agent Store connection；
  启动时还会用 `@@max_connections` 校验 `DATABASE_MANAGEMENT_CONNECTION_RESERVE`
- `MESSAGING_RETENTION_SECONDS` 默认 `86400`，只定义终态后的网络重播窗口；`0` 关闭自动过期
- 容器日志默认只写 stdout；设置 `LOG_FILE_ENABLED=true` 后写入持久日志卷
- `dist/` 保存本次发布依赖与 wheel；`build/` 和 `.egg-info` 在构建后删除
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
