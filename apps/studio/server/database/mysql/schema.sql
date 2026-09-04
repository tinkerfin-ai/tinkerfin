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
