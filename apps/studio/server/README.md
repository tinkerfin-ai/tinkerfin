# TinkerFin Studio 后端

## 是什么

该应用提供认证、数据库模型目录、Deep Agents 会话、AG-UI SSE、HITL、历史恢复、
远程取消和用户级 OpenSandbox。会话事件先提交到 Redis Messaging，再按相同序号
投影到 MySQL。

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

本地进程使用应用目录的 `.env`，其中 MySQL、Redis 和 OpenSandbox 地址必须能从宿主机
访问。MySQL 空库结构见 [database/mysql/schema.sql](database/mysql/schema.sql)。

## 单机部署

要求 Bash、OpenSSL、uv、Docker 和 Docker Compose 2.24 或更高版本。在仓库根目录
执行：

```bash
./apps/studio/server/deploy/setup.sh
./apps/studio/server/deploy/deploy.sh
```

`setup.sh` 创建部署专用 `.env` 和权限为 `0600` 的文件型 Secrets。默认部署启动
Studio、MySQL、Redis、OpenSandbox 和一次性数据库初始化服务，只向宿主机回环地址
发布 Studio 端口。

创建管理员与默认模型：

```bash
./apps/studio/server/deploy/manage.sh \
  user create --username admin --display-name Admin --role admin

./apps/studio/server/deploy/manage.sh \
  model upsert \
  --model-id main \
  --display-name Main \
  --provider openai \
  --model-name provider-model \
  --base-url https://models.example.com/v1 \
  --default
```

密码和模型 API key 缺省使用隐藏交互输入，不进入命令历史。模型 API key 当前以明文
保存在 `agent_models` 表，必须限制数据库账号、日志和备份访问。

## 外部依赖模式

编辑 `deploy/.env` 与 `deploy/secrets/`，把数据库 URL、Redis 地址和 OpenSandbox 地址
改为容器可达的外部服务，然后执行：

```bash
./apps/studio/server/deploy/deploy.sh --external
```

外部空库可使用 Compose 工具服务执行全量初始化；如果数据库只存在部分 Studio 表，
初始化会拒绝继续，不会覆盖数据：

```bash
docker compose \
  --env-file apps/studio/server/deploy/.env \
  -f apps/studio/server/deploy/docker-compose.yaml \
  --profile tools run --rm database-init
```

## 运行约束

- `/health/live` 只表示进程存活
- `/health/ready` 检查 MySQL、Redis 与 OpenSandbox，就绪失败返回 503
- 容器日志默认只写 stdout；设置 `LOG_FILE_ENABLED=true` 后写入持久日志卷
- `dist/` 保存本次发布依赖与 wheel；`build/` 和 `.egg-info` 在构建后删除
- `docker compose down -v` 会永久删除 MySQL、Redis、OpenSandbox 和日志卷数据
- 文件型 Secrets 优先于普通环境变量；缺失、不可读或内容为空会阻止启动

## 验证

```bash
uv run pytest -q apps/studio/server/tests
uv run ruff check apps/studio/server
uv run pyright apps/studio/server/src apps/studio/server/tests
```

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。
