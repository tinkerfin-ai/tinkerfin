SET NAMES utf8mb4;

CREATE TABLE users (
  id INTEGER NOT NULL AUTO_INCREMENT COMMENT '用户主键',
  username VARCHAR(64) NOT NULL COMMENT '登录用户名',
  display_name VARCHAR(128) NOT NULL COMMENT '展示名称',
  password_hash VARCHAR(512) NOT NULL COMMENT '带算法、参数和独立盐值的密码哈希',
  roles JSON NOT NULL COMMENT '用户角色列表',
  disabled BOOL NOT NULL COMMENT '是否禁止登录',
  CONSTRAINT pk_users PRIMARY KEY (id),
  UNIQUE KEY ix_users_username (username)
) COMMENT='TinkerFin Studio 登录用户';

CREATE TABLE agent_models (
  id INTEGER NOT NULL AUTO_INCREMENT COMMENT '模型配置主键',
  model_id VARCHAR(64) NOT NULL COMMENT '前后端使用的稳定模型 ID',
  display_name VARCHAR(128) NOT NULL COMMENT '前端展示名称',
  provider VARCHAR(32) NOT NULL COMMENT '已安装的 LangChain provider：deepseek 或 openai',
  model_name VARCHAR(128) NOT NULL COMMENT '供应商实际模型名称',
  base_url VARCHAR(1024) NOT NULL COMMENT '模型服务 API 基础地址',
  api_key TEXT NOT NULL COMMENT '模型服务明文 API 密钥，禁止通过接口或日志暴露',
  reasoning_enabled BOOL NOT NULL COMMENT '是否启用已验证的 provider reasoning 参数',
  enabled BOOL NOT NULL COMMENT '是否允许创建新 run',
  is_default BOOL NOT NULL COMMENT '是否为前端默认模型，由应用事务保证唯一',
  sort_order INTEGER NOT NULL COMMENT '模型目录升序排序值',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  CONSTRAINT pk_agent_models PRIMARY KEY (id),
  CONSTRAINT uq_agent_models_model_id UNIQUE (model_id),
  KEY ix_agent_models_default (is_default, enabled),
  KEY ix_agent_models_enabled_order (enabled, sort_order, id)
) COMMENT='可由前端选择的 Agent 模型与连接配置';

CREATE TABLE conversation_threads (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '会话主键',
  user_id BIGINT NOT NULL COMMENT '所属用户 ID，由应用层保证存在',
  thread_id VARCHAR(128) NOT NULL COMMENT '外部 AG-UI threadId',
  title VARCHAR(255) NOT NULL COMMENT '会话标题',
  status VARCHAR(32) NOT NULL COMMENT '会话状态：idle/running/waiting_approval/error/deleting',
  last_run_id VARCHAR(128) COMMENT '最近主 run ID',
  last_model VARCHAR(64) COMMENT '最近主 run 使用的稳定模型 ID',
  last_seq BIGINT NOT NULL COMMENT 'Messaging 会话流最新已投影序号',
  snapshot_seq BIGINT NOT NULL COMMENT 'v3 快照覆盖到的序号',
  snapshot_version INTEGER NOT NULL COMMENT '快照结构版本，当前固定为 3',
  message_count INTEGER NOT NULL COMMENT 'user 与 assistant 消息数量',
  tool_call_count INTEGER NOT NULL COMMENT 'Tool 调用开始事件累计数',
  has_pending_interrupt BOOL NOT NULL COMMENT '是否存在待处理审批',
  pinned BOOL NOT NULL COMMENT '是否置顶',
  snapshot_json JSON COMMENT '按 snapshot_seq 生成的完整前端恢复快照',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  deleted_at DATETIME COMMENT '软删除时间',
  CONSTRAINT pk_conversation_threads PRIMARY KEY (id),
  CONSTRAINT ck_conversation_threads_snapshot_version CHECK (snapshot_version = 3),
  CONSTRAINT uq_conversation_threads_user_thread UNIQUE (user_id, thread_id),
  KEY ix_conversation_threads_user_pinned_updated (user_id, deleted_at, pinned, updated_at, id),
  KEY ix_conversation_threads_user_status_updated (user_id, status, updated_at, id),
  KEY ix_conversation_threads_user_updated (user_id, deleted_at, updated_at, id)
) COMMENT='用户会话元信息与最新可信前端快照';

CREATE TABLE conversation_runs (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT 'run 投影主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  run_id VARCHAR(128) NOT NULL COMMENT '主请求 runId 或 subagentInvocationId',
  parent_run_id VARCHAR(128) COMMENT '标准 AG-UI parentRunId',
  origin_main_run_id VARCHAR(128) COMMENT '首次发现子执行的主请求 run ID',
  last_main_run_id VARCHAR(128) COMMENT '最近承载子执行事件的主请求 run ID',
  agent_type VARCHAR(32) NOT NULL COMMENT '运行主体：main 或 subagent',
  agent_name VARCHAR(128) COMMENT '运行主体名称',
  graph_task_id VARCHAR(128) COMMENT '子图 runtime task ID',
  model_id VARCHAR(64) COMMENT '主 run 使用的稳定模型 ID',
  status VARCHAR(32) NOT NULL COMMENT 'run 状态：running/success/interrupt/error/cancelled',
  input_json JSON COMMENT '主 run 的完整请求输入',
  config_json JSON COMMENT 'checkpoint、模型与 resume 配置投影',
  outcome_json JSON COMMENT '最终 AG-UI outcome 或 error',
  started_at DATETIME NOT NULL COMMENT '开始时间',
  finished_at DATETIME COMMENT '结束时间',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  CONSTRAINT pk_conversation_runs PRIMARY KEY (id),
  CONSTRAINT uq_conversation_runs_thread_run UNIQUE (conversation_thread_id, run_id),
  KEY ix_conversation_runs_thread_last_main_status (conversation_thread_id, last_main_run_id, status),
  KEY ix_conversation_runs_thread_started (conversation_thread_id, started_at, id),
  KEY ix_conversation_runs_thread_status (conversation_thread_id, status, updated_at)
) COMMENT='主 Agent 与子 Agent 运行投影';

CREATE TABLE conversation_events (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '事件主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  run_id VARCHAR(128) NOT NULL COMMENT 'Messaging producer run ID',
  seq BIGINT NOT NULL COMMENT '与 SSE id 一致的严格递增序号',
  event_id VARCHAR(255) NOT NULL COMMENT 'Messaging 幂等 message ID',
  event_type VARCHAR(64) NOT NULL COMMENT 'AG-UI 事件类型',
  schema_version INTEGER NOT NULL COMMENT 'Studio 事件结构版本，当前固定为 3',
  protocol_version VARCHAR(32) NOT NULL COMMENT 'AG-UI 协议版本',
  event_json JSON NOT NULL COMMENT 'AG-UI 事件 JSON',
  event_text LONGTEXT NOT NULL COMMENT '与 SSE data 完全一致的 UTF-8 JSON',
  created_at DATETIME NOT NULL COMMENT 'Redis 首次提交时间',
  CONSTRAINT pk_conversation_events PRIMARY KEY (id),
  CONSTRAINT ck_conversation_events_schema_version CHECK (schema_version = 3),
  CONSTRAINT uq_conversation_events_thread_seq UNIQUE (conversation_thread_id, seq),
  CONSTRAINT uq_conversation_events_thread_event UNIQUE (conversation_thread_id, event_id),
  KEY ix_conversation_events_thread_run_seq (conversation_thread_id, run_id, seq)
) COMMENT='Redis Messaging 已提交 AG-UI 事件的 MySQL 事实副本';

CREATE TABLE conversation_interrupts (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT 'interrupt 投影主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  run_id VARCHAR(128) NOT NULL COMMENT '产生 interrupt 的主 run ID',
  resolved_run_id VARCHAR(128) COMMENT '认领或处理该 interrupt 的 resume run ID',
  interrupt_id VARCHAR(255) NOT NULL COMMENT 'AG-UI 公共 interrupt ID',
  status VARCHAR(32) NOT NULL COMMENT 'pending/resolved/cancelled',
  reason VARCHAR(64) NOT NULL COMMENT 'AG-UI interrupt reason',
  message TEXT COMMENT '前端审批提示',
  request_json JSON NOT NULL COMMENT '完整公开 interrupt 请求',
  resume_json JSON COMMENT '对应 resume 条目',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  resolved_at DATETIME COMMENT '解决时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  CONSTRAINT pk_conversation_interrupts PRIMARY KEY (id),
  CONSTRAINT uq_conversation_interrupts_thread_interrupt UNIQUE (conversation_thread_id, interrupt_id),
  KEY ix_conversation_interrupts_thread_status (conversation_thread_id, status, updated_at)
) COMMENT='前端可恢复的 HITL interrupt 投影';

CREATE TABLE conversation_messages (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '消息投影主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  run_id VARCHAR(128) COMMENT '消息所属 run ID',
  message_id VARCHAR(255) NOT NULL COMMENT 'AG-UI message ID',
  `role` VARCHAR(32) NOT NULL COMMENT 'user/assistant/tool/subagent/process/error',
  content TEXT NOT NULL COMMENT '聚合后的可见文本',
  status VARCHAR(32) NOT NULL COMMENT 'streaming/completed/failed/paused',
  meta_json JSON COMMENT 'Tool、Agent 与运行关联元数据',
  first_seq BIGINT NOT NULL COMMENT '首次出现的事件序号',
  last_seq BIGINT NOT NULL COMMENT '最后更新的事件序号',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  CONSTRAINT pk_conversation_messages PRIMARY KEY (id),
  CONSTRAINT uq_conversation_messages_thread_message UNIQUE (conversation_thread_id, message_id),
  KEY ix_conversation_messages_thread_role_updated (conversation_thread_id, `role`, updated_at, id)
) COMMENT='用于历史恢复、检索和审计的消息聚合投影';

CREATE TABLE store_migrations (
  v INTEGER NOT NULL COMMENT 'Store migration 版本',
  CONSTRAINT pk_store_migrations PRIMARY KEY (v)
) COMMENT='LangGraph MySQL Store 已应用迁移版本';

CREATE TABLE store (
  prefix VARCHAR(500) NOT NULL COMMENT 'Store 文档命名空间',
  `key` VARCHAR(150) NOT NULL COMMENT '命名空间内文档键',
  value JSON NOT NULL COMMENT '文档 JSON 内容',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT pk_store PRIMARY KEY (prefix, `key`),
  KEY store_prefix_idx (prefix)
) COMMENT='Deep Agents 长期 memory Store';

INSERT INTO store_migrations (v) VALUES (0), (1);

CREATE TABLE tinkerfin_opensandbox_schema_versions (
  component VARCHAR(64) NOT NULL COMMENT '持久化组件名称',
  version INTEGER NOT NULL COMMENT '最新完整提交的 schema 版本',
  CONSTRAINT pk_tinkerfin_opensandbox_schema_versions PRIMARY KEY (component)
) COMMENT='OpenSandbox State schema 版本';

CREATE TABLE tinkerfin_opensandbox_owners (
  namespace VARCHAR(64) NOT NULL COMMENT 'OpenSandbox State 逻辑部署命名空间',
  owner_digest VARCHAR(43) NOT NULL COMMENT '命名空间与业务 owner key 的 URL-safe SHA-256',
  sandbox_id VARCHAR(255) COMMENT '当前权威 Sandbox ID',
  binding_generation BIGINT NOT NULL DEFAULT 0 COMMENT '提交当前绑定的 fencing generation',
  generation BIGINT NOT NULL DEFAULT 0 COMMENT 'owner 转换单调 generation',
  claim_token VARCHAR(32) COMMENT '当前变更 owner 的不透明 claim token',
  lease_expires_at DATETIME COMMENT '当前 claim 的 UTC 到期时间',
  updated_at DATETIME NOT NULL COMMENT '最近 owner 状态更新时间',
  CONSTRAINT pk_tinkerfin_opensandbox_owners PRIMARY KEY (namespace, owner_digest),
  KEY ix_tinkerfin_opensandbox_owners_lease (namespace, lease_expires_at)
) COMMENT='权威用户 Sandbox 绑定与 fencing 状态';

CREATE TABLE tinkerfin_opensandbox_workers (
  namespace VARCHAR(64) NOT NULL COMMENT 'OpenSandbox State 逻辑部署命名空间',
  worker_id VARCHAR(32) NOT NULL COMMENT '一个活跃 State 实例的不透明 ID',
  warm_pool_size INTEGER NOT NULL COMMENT '该 Worker 请求的全局预热容量',
  lease_expires_at DATETIME NOT NULL COMMENT 'Worker 租约 UTC 到期时间',
  updated_at DATETIME NOT NULL COMMENT 'Worker 最近续约时间',
  CONSTRAINT pk_tinkerfin_opensandbox_workers PRIMARY KEY (namespace, worker_id),
  KEY ix_tinkerfin_opensandbox_workers_lease (namespace, lease_expires_at)
) COMMENT='活跃 OpenSandbox State Worker 与预热池容量约定';

CREATE TABLE tinkerfin_opensandbox_warm_slots (
  namespace VARCHAR(64) NOT NULL COMMENT 'OpenSandbox State 逻辑部署命名空间',
  slot INTEGER NOT NULL COMMENT '全局预热池槽位序号',
  sandbox_id VARCHAR(255) COMMENT '当前槽位持有的 ready Sandbox ID',
  generation BIGINT NOT NULL DEFAULT 0 COMMENT '槽位 fencing generation',
  claim_token VARCHAR(32) COMMENT '当前填充槽位的 claim token',
  lease_expires_at DATETIME COMMENT '槽位 claim UTC 到期时间',
  updated_at DATETIME NOT NULL COMMENT '槽位最近更新时间',
  CONSTRAINT pk_tinkerfin_opensandbox_warm_slots PRIMARY KEY (namespace, slot),
  KEY ix_tinkerfin_opensandbox_warm_slots_available (namespace, sandbox_id, lease_expires_at)
) COMMENT='数据库全局 OpenSandbox 预热槽位';

CREATE TABLE tinkerfin_opensandbox_cleanup (
  namespace VARCHAR(64) NOT NULL COMMENT 'OpenSandbox State 逻辑部署命名空间',
  sandbox_id VARCHAR(255) NOT NULL COMMENT '等待确认销毁的孤立 Sandbox ID',
  generation BIGINT NOT NULL DEFAULT 0 COMMENT 'cleanup fencing generation',
  claim_token VARCHAR(32) COMMENT '当前销毁任务 claim token',
  lease_expires_at DATETIME COMMENT 'cleanup claim UTC 到期时间',
  attempts INTEGER NOT NULL DEFAULT 0 COMMENT '该目标已被领取的次数',
  created_at DATETIME NOT NULL COMMENT '首次加入 cleanup 队列时间',
  updated_at DATETIME NOT NULL COMMENT '最近 cleanup 状态更新时间',
  CONSTRAINT pk_tinkerfin_opensandbox_cleanup PRIMARY KEY (namespace, sandbox_id),
  KEY ix_tinkerfin_opensandbox_cleanup_lease (namespace, lease_expires_at)
) COMMENT='失败 Sandbox 销毁任务的持久化重试队列';

INSERT INTO tinkerfin_opensandbox_schema_versions (component, version)
VALUES ('opensandbox-state', 2);
