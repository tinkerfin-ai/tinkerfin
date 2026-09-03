SET NAMES utf8mb4;

CREATE TABLE users (
  id INTEGER NOT NULL AUTO_INCREMENT COMMENT '用户主键',
  username VARCHAR(64) NOT NULL COMMENT '登录用户名',
  display_name VARCHAR(128) NOT NULL COMMENT '展示名称',
  avatar_url VARCHAR(2048) COMMENT '头像 HTTPS URL',
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
  thread_id VARCHAR(128) NOT NULL COMMENT '公开 AG-UI 与 Trace 共用的 threadId',
  title VARCHAR(255) NOT NULL COMMENT '会话标题',
  status VARCHAR(32) NOT NULL COMMENT '列表状态：idle/running/waiting_approval/error/deleting',
  last_run_id VARCHAR(128) COMMENT '最近主 Run ID',
  last_model VARCHAR(64) COMMENT '最近主 Run 使用的稳定模型 ID',
  message_count INTEGER NOT NULL COMMENT 'Trace 中 user 与 assistant 消息数',
  tool_call_count INTEGER NOT NULL COMMENT 'Trace 中 Tool proposal 数',
  has_pending_interrupt BOOL NOT NULL COMMENT 'Trace 是否存在待处理交互',
  pending_interaction_kind VARCHAR(64) COMMENT 'Trace 待处理交互的产品类型',
  pinned BOOL NOT NULL COMMENT '是否置顶',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '最近 Trace 或用户会话活动时间',
  deleted_at DATETIME COMMENT '软删除时间',
  CONSTRAINT pk_conversation_threads PRIMARY KEY (id),
  CONSTRAINT uq_conversation_threads_thread UNIQUE (thread_id),
  KEY ix_conversation_threads_user_pinned_updated (user_id, deleted_at, pinned, updated_at, id),
  KEY ix_conversation_threads_user_status_updated (user_id, status, updated_at, id),
  KEY ix_conversation_threads_user_updated (user_id, deleted_at, updated_at, id)
) COMMENT='用户会话归属、产品控制与 Trace 列表摘要';

CREATE TABLE conversation_run_registrations (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT 'Run 注册主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  run_id VARCHAR(128) NOT NULL COMMENT '公开且幂等的主 Run ID',
  parent_run_id VARCHAR(128) COMMENT 'branch 或 resume 来源 Run ID',
  model_id VARCHAR(64) NOT NULL COMMENT '主 Run 使用的稳定模型 ID',
  status VARCHAR(32) NOT NULL COMMENT 'preparing/starting/running/waiting/succeeded/failed/cancelled/abandoned',
  input_json JSON NOT NULL COMMENT '用于同 runId 幂等核验的标准请求',
  terminal_outcome VARCHAR(32) COMMENT 'Trace 终态结果',
  error_code VARCHAR(128) COMMENT '客户端安全的终态错误码',
  started_at DATETIME NOT NULL COMMENT '请求注册时间',
  finished_at DATETIME COMMENT 'Trace 终态时间',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '最近业务状态更新时间',
  CONSTRAINT pk_conversation_run_registrations PRIMARY KEY (id),
  CONSTRAINT uq_conversation_run_registrations_thread_run UNIQUE (conversation_thread_id, run_id),
  KEY ix_conversation_run_registrations_thread_started (conversation_thread_id, started_at, id),
  KEY ix_conversation_run_registrations_thread_status (conversation_thread_id, status, updated_at)
) COMMENT='主 Run 请求幂等、模型与业务状态注册';

CREATE TABLE conversation_interrupt_claims (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '认领主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  interrupt_id VARCHAR(255) NOT NULL COMMENT '框架从 Checkpointer 解析的公开 interrupt ID',
  source_run_id VARCHAR(128) NOT NULL COMMENT '产生 interrupt 的来源 Run ID',
  claimed_run_id VARCHAR(128) NOT NULL COMMENT '原子认领该 interrupt 的 resume Run ID',
  status VARCHAR(32) NOT NULL COMMENT 'claimed/resolved/cancelled',
  resolution_id VARCHAR(128) COMMENT 'checkpoint marker 或 Trace abandonment 证据',
  created_at DATETIME NOT NULL COMMENT '认领创建时间',
  resolved_at DATETIME COMMENT '框架确认恢复 checkpoint 的时间',
  updated_at DATETIME NOT NULL COMMENT '最近状态更新时间',
  CONSTRAINT pk_conversation_interrupt_claims PRIMARY KEY (id),
  CONSTRAINT uq_conversation_interrupt_claims_thread_interrupt UNIQUE (conversation_thread_id, interrupt_id),
  KEY ix_conversation_interrupt_claims_run_status (conversation_thread_id, claimed_run_id, status)
) COMMENT='由框架恢复事实驱动的 interrupt 原子认领与结算';

CREATE TABLE tinkerfin_trace_events (
  namespace_hash BINARY(32) NOT NULL COMMENT 'SHA-256 namespace key',
  thread_hash BINARY(32) NOT NULL COMMENT 'SHA-256 thread key',
  generation VARCHAR(64) NOT NULL COMMENT 'Exact Trace generation ID',
  trace_seq BIGINT NOT NULL COMMENT 'One-based global sequence in the generation',
  event_id VARCHAR(64) NOT NULL COMMENT 'Idempotent Store event identity',
  run_hash BINARY(32) NOT NULL COMMENT 'SHA-256 Run key',
  run_id TEXT NOT NULL COMMENT 'Semantic Run owning this fact',
  fact_kind VARCHAR(64) NOT NULL COMMENT 'Queryable current semantic fact discriminator',
  occurred_at DATETIME(6) NOT NULL COMMENT 'Source UTC fact timestamp',
  payload LONGBLOB NOT NULL COMMENT 'Opaque canonical fact payload bytes',
  payload_digest VARCHAR(64) NOT NULL COMMENT 'SHA-256 of canonical pre-storage payload',
  persisted_bytes BIGINT NOT NULL COMMENT 'Measured current TraceEvent encoded bytes',
  created_at DATETIME(6) NOT NULL COMMENT 'Database UTC commit time',
  CONSTRAINT pk_tinkerfin_trace_events PRIMARY KEY (namespace_hash, thread_hash, generation, trace_seq),
  UNIQUE KEY uq_tinkerfin_trace_events_id (namespace_hash, event_id),
  KEY ix_tinkerfin_trace_events_run (namespace_hash, thread_hash, generation, run_hash, trace_seq)
) COMMENT='Authoritative semantic Trace Ledger events';

CREATE TABLE tinkerfin_trace_graph_nodes (
  namespace_hash BINARY(32) NOT NULL COMMENT 'SHA-256 namespace key',
  thread_hash BINARY(32) NOT NULL COMMENT 'SHA-256 thread key',
  generation VARCHAR(64) NOT NULL COMMENT 'Exact Trace generation ID',
  node_hash BINARY(32) NOT NULL COMMENT 'SHA-256 Graph node key',
  node_id TEXT NOT NULL COMMENT 'Canonical Graph node identity',
  structural_parent_hash BINARY(32) COMMENT 'SHA-256 structural parent key when present',
  structural_parent_id TEXT COMMENT 'Evidence parent before visible-node projection',
  kind VARCHAR(32) COMMENT 'Graph node kind, null only on a removal revision',
  status VARCHAR(32) COMMENT 'Graph node status, null only on a removal revision',
  name_hash BINARY(32) COMMENT 'SHA-256 display-name key, null on a removal revision',
  name TEXT COMMENT 'Graph node display name, null on a removal revision',
  run_hash BINARY(32) NOT NULL COMMENT 'SHA-256 Run revision key',
  run_id TEXT NOT NULL COMMENT 'Semantic Run owning the node',
  removed BOOL NOT NULL COMMENT 'Whether this Run revision hides an inherited node',
  graph_namespace_hash BINARY(32) COMMENT 'SHA-256 graph namespace key, null on a removal revision',
  graph_namespace TEXT COMMENT 'Canonical JSON graph namespace, null on a removal revision',
  agent_hash BINARY(32) COMMENT 'SHA-256 Agent name key when present',
  agent_name TEXT COMMENT 'Named Agent when present',
  provider_hash BINARY(32) COMMENT 'SHA-256 model provider key when present',
  provider TEXT COMMENT 'Model provider when present',
  model_hash BINARY(32) COMMENT 'SHA-256 model name key when present',
  model TEXT COMMENT 'Model name when present',
  started_at DATETIME(6) COMMENT 'Source UTC node start time, null on a removal revision',
  first_output_at DATETIME(6) COMMENT 'Source UTC first model output time',
  completed_at DATETIME(6) COMMENT 'Source UTC node completion time',
  started_seq BIGINT COMMENT 'Ledger node-creation sequence, null on a removal revision',
  updated_seq BIGINT NOT NULL COMMENT 'Ledger sequence containing the current lifecycle update',
  request_seq BIGINT COMMENT 'Ledger sequence containing current request details',
  result_seq BIGINT COMMENT 'Ledger sequence containing current result details',
  failure_seq BIGINT COMMENT 'Ledger sequence containing authoritative failure details',
  link_issue VARCHAR(64) COMMENT 'Missing evidence that prevented one exact relationship',
  CONSTRAINT pk_tinkerfin_trace_graph_nodes PRIMARY KEY (namespace_hash, thread_hash, generation, node_hash, run_hash),
  KEY ix_tinkerfin_trace_graph_run (namespace_hash, thread_hash, generation, run_hash, node_hash, updated_seq)
) COMMENT='Disposable payload-free canonical Trace Graph index';

CREATE TABLE tinkerfin_trace_namespaces (
  namespace_hash BINARY(32) NOT NULL COMMENT 'SHA-256 key for the logical namespace',
  namespace TEXT NOT NULL COMMENT 'Logical Trace Store namespace',
  created_at DATETIME(6) NOT NULL COMMENT 'Database UTC creation time',
  CONSTRAINT pk_tinkerfin_trace_namespaces PRIMARY KEY (namespace_hash)
) COMMENT='Trace namespace ownership';

CREATE TABLE tinkerfin_trace_projection_checkpoints (
  namespace_hash BINARY(32) NOT NULL COMMENT 'SHA-256 namespace key',
  thread_hash BINARY(32) NOT NULL COMMENT 'SHA-256 thread key',
  generation VARCHAR(64) NOT NULL COMMENT 'Exact Trace generation ID',
  projection_hash BINARY(32) NOT NULL COMMENT 'SHA-256 Projection key',
  run_scope_hash BINARY(32) NOT NULL COMMENT 'SHA-256 Run scope key',
  projection_name TEXT NOT NULL COMMENT 'Canonical Projection identity',
  run_scope TEXT NOT NULL COMMENT 'Run ID or empty thread-wide scope',
  as_of_seq BIGINT NOT NULL COMMENT 'Fixed Ledger prefix represented by state',
  state_payload LONGBLOB NOT NULL COMMENT 'Opaque canonical Projection state bytes',
  state_digest VARCHAR(64) NOT NULL COMMENT 'SHA-256 of canonical pre-storage state',
  created_at DATETIME(6) NOT NULL COMMENT 'Database UTC checkpoint commit time',
  CONSTRAINT pk_tinkerfin_trace_projection_checkpoints PRIMARY KEY (
    namespace_hash,
    thread_hash,
    generation,
    projection_hash,
    run_scope_hash,
    as_of_seq
  )
) COMMENT='Disposable Projection checkpoint history';

CREATE TABLE tinkerfin_trace_threads (
  namespace_hash BINARY(32) NOT NULL COMMENT 'SHA-256 namespace key',
  thread_hash BINARY(32) NOT NULL COMMENT 'SHA-256 thread key',
  namespace TEXT NOT NULL COMMENT 'Logical Trace namespace',
  thread_id TEXT NOT NULL COMMENT 'Canonical semantic thread ID',
  generation VARCHAR(64) NOT NULL COMMENT 'Non-reusable current generation ID',
  next_seq BIGINT NOT NULL COMMENT 'Next one-based global Trace sequence',
  persisted_bytes BIGINT NOT NULL COMMENT 'Canonical encoded event bytes in this generation',
  created_at DATETIME(6) NOT NULL COMMENT 'Database UTC generation creation time',
  updated_at DATETIME(6) NOT NULL COMMENT 'Database UTC last mutation time',
  CONSTRAINT pk_tinkerfin_trace_threads PRIMARY KEY (namespace_hash, thread_hash),
  UNIQUE KEY ix_tinkerfin_trace_threads_generation (namespace_hash, generation)
) COMMENT='Current Trace generation and sequence allocator';

CREATE TABLE tinkerfin_trace_writers (
  namespace_hash BINARY(32) NOT NULL COMMENT 'SHA-256 namespace key',
  thread_hash BINARY(32) NOT NULL COMMENT 'SHA-256 thread key',
  generation VARCHAR(64) NOT NULL COMMENT 'Exact Trace generation ID',
  run_hash BINARY(32) NOT NULL COMMENT 'SHA-256 Run key',
  run_id TEXT NOT NULL COMMENT 'Canonical semantic Run ID',
  owner_token VARCHAR(64) NOT NULL COMMENT 'Opaque current writer ownership token',
  fence BIGINT NOT NULL COMMENT 'Monotonic writer fencing token',
  lease_expires_at DATETIME(6) NOT NULL COMMENT 'Database-clock writer lease deadline',
  active BOOL NOT NULL COMMENT 'Whether the writer may still append',
  terminal_committed BOOL NOT NULL COMMENT 'Whether the exactly-once Run terminal fact is committed',
  closed_committed BOOL NOT NULL COMMENT 'Whether the Runtime cleanup completion fact is committed',
  committed_events BIGINT NOT NULL COMMENT 'Events committed by this Run writer',
  remaining_event_reserve BIGINT NOT NULL COMMENT 'Reserved terminal event capacity',
  remaining_byte_reserve BIGINT NOT NULL COMMENT 'Reserved terminal canonical bytes',
  created_at DATETIME(6) NOT NULL COMMENT 'Database UTC first writer creation time',
  updated_at DATETIME(6) NOT NULL COMMENT 'Database UTC last ownership mutation time',
  CONSTRAINT pk_tinkerfin_trace_writers PRIMARY KEY (
    namespace_hash,
    thread_hash,
    generation,
    run_hash
  ),
  KEY ix_tinkerfin_trace_writers_active_lease (
    namespace_hash,
    thread_hash,
    generation,
    active,
    lease_expires_at
  )
) COMMENT='Exclusive Run writer ownership, lease, fence, and terminal reserve';

CREATE TABLE store (
  prefix VARCHAR(500) NOT NULL COMMENT 'Store document namespace',
  `key` VARCHAR(150) NOT NULL COMMENT 'Document key within the namespace',
  value JSON NOT NULL COMMENT 'Document JSON value',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'Document creation time',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'Document update time',
  CONSTRAINT pk_store PRIMARY KEY (prefix, `key`),
  KEY store_prefix_idx (prefix)
) COMMENT='Deep Agents long-term memory Store';

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
